#!/usr/bin/python
# -*- coding: utf-8 -*-
#
# antbs.py
#
# Copyright © 2013-2016 Antergos
#
# This file is part of The Antergos Build Server, (AntBS).
#
# AntBS is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# AntBS is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# The following additional terms are in effect as per Section 7 of the license:
#
# The preservation of all legal notices and author attributions in
# the material or in the Appropriate Legal Notices displayed
# by works containing it is required.
#
# You should have received a copy of the GNU General Public License
# along with AntBS; If not, see <http://www.gnu.org/licenses/>.


""" AntBS (Antergos Build Server) Main Module """

# Start ignoring PyImportSortBear as monkey patching needs to be done before
# other imports
import gevent
import gevent.monkey

gevent.monkey.patch_all()
# Stop ignoring

import glob
import json
import os
import re
from datetime import datetime, timedelta

import requests

from flask import (
    Flask, Response, abort, flash, redirect, render_template, request, url_for
)

from flask.ext.stormpath import StormpathManager, groups_required, user
from werkzeug.contrib.fixers import ProxyFix

from rq import Connection, Queue, Worker
import rq_dashboard

import transaction_handler
import utils.pagination
import webhook
import iso
from database.monitor import get_monitor_object, check_repos_for_changes
from database.base_objects import db
from database.build import get_build_object
from database.package import get_pkg_object
from database.server_status import get_timeline_object, status
from database.transaction import get_trans_object
from database.repo import get_repo_object
from utils.logging_config import logger, handle_exceptions
from utils.utilities import copy_or_symlink
from views.repo import repo_view


app = transaction_queue = repo_queue = webhook_queue = w1 = w2 = w3 = monitor = None


def url_for_other_page(page):
    args = request.view_args.copy()
    args['page'] = page
    return url_for(request.endpoint, **args)


def initialize_app():
    """
    Creates global flask app object and initializes settings.

    """

    global app
    app = Flask(__name__)
    handle_exceptions(app)

    # Stormpath configuration
    app.config.update({'SECRET_KEY': status.sp_session_key,
                       'STORMPATH_API_KEY_ID': status.sp_api_id,
                       'STORMPATH_API_KEY_SECRET': status.sp_api_key,
                       'STORMPATH_APPLICATION': status.sp_app,
                       'STORMPATH_ENABLE_USERNAME': True,
                       'STORMPATH_REQUIRE_USERNAME': True,
                       'STORMPATH_ENABLE_REGISTRATION': False,
                       'STORMPATH_REDIRECT_URL': '/pkg_review',
                       'STORMPATH_LOGIN_TEMPLATE': 'admin/login.html',
                       'STORMPATH_COOKIE_DURATION': timedelta(days=14),
                       'STORMPATH_ENABLE_FORGOT_PASSWORD': True})

    # Create Stormpath Manager object.
    StormpathManager(app)

    # Jinja2 configuration
    global url_for_other_page
    app.jinja_options = Flask.jinja_options.copy()
    app.jinja_options['lstrip_blocks'] = True
    app.jinja_options['trim_blocks'] = True
    app.jinja_env.globals['url_for_other_page'] = url_for_other_page

    # Use gunicorn with nginx proxy
    app.wsgi_app = ProxyFix(app.wsgi_app)

    # Setup rq_dashboard (accessible at '/rq' endpoint)
    app.config.from_object(rq_dashboard.default_settings)
    app.register_blueprint(rq_dashboard.blueprint, url_prefix='/rq')

    # Register our views
    app.register_blueprint(repo_view, url_prefix='/repo')

    # Setup rq (background task queue manager)
    with Connection(db):
        global transaction_queue, repo_queue, webhook_queue, w1, w2, w3
        transaction_queue = Queue('transactions')
        repo_queue = Queue('update_repo')
        webhook_queue = Queue('webook')
        w1 = Worker([transaction_queue])
        w2 = Worker([repo_queue])
        w3 = Worker([webhook_queue])


# Make `app` available to gunicorn
initialize_app()


@app.before_request
def maybe_check_for_remote_commits():
    global monitor

    if monitor is None:
        monitor = get_monitor_object(name='github')

    check_expired = monitor.__is_expired__('checked_recently')

    if not monitor.checked_recently or check_expired:
        repo_queue.enqueue_call(check_repos_for_changes, args=('github',))

    if '/rq' in request.path and not user.is_authenticated():
        abort(403)


@app.context_processor
def inject_global_template_variables():
    return dict(
        idle=status.idle,
        current_status=status.current_status,
        now_building=status.now_building,
        rev_pending=status.pending_review,
        user=user
    )


def get_live_build_output(bnum):
    psub = db.pubsub()
    psub.subscribe('live:build_output:{0}'.format(bnum))
    last_line_key = 'tmp:build_log_last_line:{0}'.format(bnum)
    first_run = True
    keep_alive = 0
    while True:
        message = psub.get_message()
        if message:
            if first_run:
                message['data'] = db.get(last_line_key)
                first_run = False

            if message['data'] not in ['1', 1]:
                yield 'event: build_output\ndata: {0}\n\n'.format(message['data']).encode('UTF-8')

        elif keep_alive > 560:
            keep_alive = 0
            yield ':'.encode('UTF-8')

        keep_alive += 1
        gevent.sleep(.05)

    psub.close()


def get_live_status_updates():
    last_event = None
    keep_alive = 0
    while True:
        if status.idle and 'Idle' != last_event:
            last_event = 'Idle'
            yield 'event: status\ndata: {0}\n\n'.format('Idle').encode('UTF-8')
        elif not status.idle and status.current_status != last_event:
            last_event = status.current_status
            yield 'event: status\ndata: {0}\n\n'.format(status.current_status).encode('UTF-8')
        elif keep_alive > 15:
            keep_alive = 0
            yield ':'.encode('UTF-8')

        keep_alive += 1
        gevent.sleep(1)


def get_paginated(item_list, per_page, page, timeline):
    if len(item_list) < 1:
        return item_list, 0
    page -= 1
    items = list(item_list)
    items.reverse()
    paginated = [items[i:i + per_page] for i in range(0, len(items), per_page)]
    all_pages = len(paginated)
    if all_pages and page <= all_pages:
        this_page = paginated[page]
    elif all_pages and page > all_pages:
        this_page = paginated[-1]
    else:
        this_page = paginated[0]

    return this_page, all_pages


def match_pkg_name_build_log(bnum=None, match=None):
    if not bnum or not match:
        return False
    pname = get_build_object(bnum=bnum)
    logger.info(bnum)
    if pname:
        return match in pname.pkgname
    else:
        return False


def get_repo_packages_in_group(package_group, repo_name):
    pkgs = []
    repo = get_repo_object(repo_name)

    for pkg in repo.packages:
        pkg_obj = get_pkg_object(pkg)

        if package_group in pkg_obj.groups:
            pkgs.append(pkg_obj)

    return pkgs


def set_pkg_review_result(bnum=False, dev=False, result=False):
    if not any([bnum, dev, result]):
        abort(500)

    errmsg = dict(error=True, msg=None)
    dt = datetime.now().strftime("%m/%d/%Y %I:%M%p")

    try:
        bld_obj = get_build_object(bnum=bnum)
        pkg_obj = get_pkg_object(name=bld_obj.pkgname)
        if pkg_obj and bld_obj:
            allowed = pkg_obj.allowed_in
            if 'main' not in allowed and result == 'passed':
                msg = '{0} is not allowed in main repo.'.format(pkg_obj.pkgname)
                errmsg.update(error=True, msg=msg)
                return errmsg
            else:
                bld_obj.review_dev = dev
                bld_obj.review_date = dt
                bld_obj.review_status = result

        if result == 'skip':
            errmsg = dict(error=False, msg=None)
            return errmsg

        glob_string_64 = '{0}/**/{1}-***'.format(status.STAGING_64, pkg_obj.pkgname)
        glob_string_32 = '{0}/**/{1}-***'.format(status.STAGING_32, pkg_obj.pkgname)
        pkg_files_64 = glob.glob(glob_string_64, recursive=True)
        pkg_files_32 = glob.glob(glob_string_32, recursive=True)
        pkg_files = pkg_files_64 + pkg_files_32

        if pkg_obj.is_split_package and pkg_obj.split_packages:
            for split_pkg in pkg_obj.split_packages:
                glob_string_64 = '{0}/**/{1}-***'.format(status.STAGING_64, split_pkg)
                glob_string_32 = '{0}/**/{1}-***'.format(status.STAGING_32, split_pkg)
                pkg_files_64.extend(glob.glob(glob_string_64, recursive=True))
                pkg_files_32.extend(glob.glob(glob_string_32, recursive=True))
                pkg_files.extend(pkg_files_64 + pkg_files_32)

        if pkg_files or True:
            for f in pkg_files_64:
                logger.debug('f in pkg_files_64 fired!')
                if result == 'passed':
                    copy_or_symlink(f, status.MAIN_64)
                    copy_or_symlink(f, '/tmp')
                if result != 'skip':
                    os.remove(f)
            for f in pkg_files_32:
                if result == 'passed':
                    copy_or_symlink(f, status.MAIN_32)
                    copy_or_symlink(f, '/tmp')
                if result != 'skip':
                    os.remove(f)
            if result and result != 'skip':
                repo_queue.enqueue_call(transaction_handler.process_dev_review,
                                        args=(bld_obj.bnum,), timeout=9600)
                errmsg = dict(error=False, msg=None)

        else:
            logger.error('While moving to main, no packages were found to move.')
            err = 'While moving to main, no packages were found to move.'
            errmsg = dict(error=True, msg=err)

    except (OSError, Exception) as err:
        logger.error('Error while moving to main: %s', err)
        err = str(err)
        errmsg = dict(error=True, msg=err)

    return errmsg


@app.errorhandler(404)
def page_not_found(e):
    return render_template('error/404.html'), 404


@app.errorhandler(500)
def internal_error(e):
    if e is not None:
        logger.error(e)
    return render_template('error/500.html'), 500


@app.errorhandler(400)
def flask_error(e):
    if e is not None:
        logger.error(e)
    return render_template('error/500.html'), 400





@app.route("/building")
@app.route("/building/<bnum>")
def building(bnum=None):
    bld_objs = {}
    selected = None

    if bnum and bnum not in status.now_building:
        abort(400)

    if status.now_building and not status.idle:
        try:
            bld_objs = {b: get_build_object(bnum=b) for b in status.now_building if b}
        except Exception as err:
            logger.error(err)
            logger.debug(bld_objs)
            abort(500)
        if not bnum or bnum not in bld_objs:
            bnum = sorted(bld_objs.keys())[0]

        selected = dict(bnum=bnum, pkgname=bld_objs[bnum].pkgname,
                        version=bld_objs[bnum].version_str, start=bld_objs[bnum].start_str,
                        container=bld_objs[bnum].container)

    return render_template('building.html', bld_objs=bld_objs, selected=selected)


@app.route('/api/get_log')
@app.route("/api/get_log/<int:bnum>")
def get_log(bnum=None):
    if status.idle or not status.now_building:
        abort(404)

    if not bnum:
        bnum = status.now_building[0]

    if not bnum:
        abort(404)

    headers = {
        'Content-Type': 'text/event-stream',
        'Cache-Control': 'no-cache',
    }
    return Response(get_live_build_output(bnum), direct_passthrough=True,
                    mimetype='text/event-stream', headers=headers)


@app.route('/api/hook', methods=['POST', 'GET'])
def hooked():
    hook = webhook.Webhook(request)
    if hook.result is int:
        abort(hook.result)
    else:
        return json.dumps(hook.result)


@app.route('/scheduled')
def scheduled():
    builds = []
    if status.build_queue and len(status.build_queue) > 0:
        for bnum in status.build_queue:
            try:
                bld_obj = get_build_object(bnum=bnum)
                builds.append(bld_obj)
            except ValueError as err:
                logger.error(err)

    return render_template("builds/scheduled.html")


@app.route('/browse/<goto>')
@app.route('/browse')
# @cache.memoize(timeout=900, unless=cache_buster)
def repo_browser(goto=None):
    building = status.now_building
    release = False
    testing = False
    main = False
    template = "repo_browser/repo_browser.html"
    if goto == 'release':
        release = True
    elif goto == 'testing':
        testing = True
    elif goto == 'main':
        main = True
        template = "repo_browser/repo_browser_main.html"

    return render_template(template, building=building, release=release, testing=testing,
                           main=main, user=user)


@app.route('/pkg_review/<int:page>')
@app.route('/pkg_review', methods=['POST', 'GET'])
@groups_required(['admin'])
def dev_pkg_check(page=None):
    build_status = 'completed'
    set_rev_error = False
    set_rev_error_msg = None
    review = True
    is_logged_in = user.is_authenticated()
    if page is None:
        page = 1
    if request.method == 'POST':
        payload = json.loads(request.data.decode('utf-8'))
        bnum = payload['bnum']
        dev = payload['dev']
        result = payload['result']
        if len([x for x in (bnum, dev, result) if x]) == 3:
            logger.debug('fired!')
            set_review = set_pkg_review_result(bnum, dev, result)
            if set_review.get('error'):
                set_rev_error = set_review.get('msg')
                message = dict(msg=set_rev_error)
                return json.dumps(message)
            else:
                message = dict(msg='ok')
                return json.dumps(message)

    completed, all_pages, rev_pending = get_build_info(page, build_status, is_logged_in)
    pagination = utils.pagination.Pagination(page, 10, len(rev_pending))
    return render_template("admin/pkg_review.html", completed=completed, all_pages=all_pages,
                           set_rev_error=set_rev_error, set_rev_error_msg=set_rev_error_msg,
                           user=user, rev_pending=rev_pending, pagination=pagination)


@app.route('/api/build_pkg_now', methods=['POST', 'GET'])
@groups_required(['admin'])
def build_pkg_now():
    if request.method == 'POST':
        pkg_obj = None
        pkgname = request.form['pkgname']
        dev = request.form['dev']

        if not pkgname:
            abort(500)

        try:
            pkg_obj = get_pkg_object(pkgname, fetch_pkgbuild=True)
        except Exception as err:
            logger.error(err)

        if pkg_obj:
            is_logged_in = user.is_authenticated()
            p, a, rev_pending = get_build_info(1, 'completed', is_logged_in)
            pending = False

            for bnum in rev_pending:
                bld_obj = get_build_object(bnum=bnum)
                if bld_obj and pkg_obj.pkgname == bld_obj.pkgname:
                    pending = True
                    break

            if pending:
                flash('Unable to build %s because it is in "pending review" status.' % pkgname,
                      category='error')
            else:
                if '-x86_64' in pkg_obj.pkgname or '-i686' in pkg_obj.pkgname:
                    status.iso_flag = True
                    if 'minimal' in pkgname:
                        status.iso_minimal = True
                    else:
                        status.iso_minimal = False

                if 'cnchi-dev' == pkgname:
                    db.set('CNCHI-DEV-OVERRIDE', True)
                trans = get_trans_object(packages=[pkgname], repo_queue=repo_queue)
                status.transaction_queue.rpush(trans.tnum)
                transaction_queue.enqueue_call(transaction_handler.handle_hook, timeout=84600)
                get_timeline_object(
                    msg='<strong>%s</strong> added <strong>%s</strong> to the build queue.' % (
                        dev, pkgname), tl_type='0')
        else:
            flash('Package not found. Has the PKGBUILD been pushed to github?', category='error')

    return redirect(redirect_url())


@app.route('/api/get_status', methods=['GET'])
@app.route('/api/ajax', methods=['GET', 'POST'])
def get_status():
    if 'get_status' in request.path:
        headers = {
            'Content-Type': 'text/event-stream',
            'Cache-Control': 'no-cache',
        }
        return Response(get_live_status_updates(), direct_passthrough=True,
                        mimetype='text/event-stream', headers=headers)

    if not user.is_authenticated():
        abort(403)

    iso_release = bool(request.args.get('do_iso_release', False))
    reset_queue = bool(request.args.get('reset_build_queue', False))
    rerun_transaction = int(request.args.get('rerun_transaction', 0))
    message = dict(msg='Ok')

    if request.method == 'POST':
        payload = json.loads(request.data.decode('UTF-8'))
        pkg = payload.get('pkg', None)
        dev = payload.get('dev', None)
        action = payload.get('result', None)

        if all(i is not None for i in (pkg, dev, action)):
            if action in ['remove']:
                repo_queue.enqueue_call(
                    transaction_handler.update_main_repo(is_action=True, action=action, action_pkg=pkg))
            elif 'rebuild' == action:
                trans_obj = get_trans_object([pkg], repo_queue=repo_queue)
                status.transaction_queue.rpush(trans_obj.tnum)
                transaction_queue.enqueue_call(transaction_handler.handle_hook, timeout=84600)
                get_timeline_object(
                    msg='<strong>%s</strong> added <strong>%s</strong> to the build queue.' % (
                        dev, pkg), tl_type='0')
            return json.dumps(message)

    if iso_release and user.is_authenticated():
        transaction_queue.enqueue_call(iso.iso_release_job)
        return json.dumps(message)

    elif reset_queue and user.is_authenticated():
        if transaction_queue.count > 0:
            transaction_queue.empty()
        if repo_queue.count > 0:
            repo_queue.empty()
        items = len(status.transaction_queue)
        if items > 0:
            for item in range(items):
                popped = status.transaction_queue.rpop()
                logger.debug(popped)
        status.idle = True
        status.current_status = 'Idle.'
        return json.dumps(message)

    elif rerun_transaction and user.is_authenticated():
        event = get_timeline_object(event_id=rerun_transaction)
        pkgs = event.packages
        if pkgs:
            _ = {}
            for pkg in pkgs:
                _[pkg] = get_pkg_object(pkg, fetch_pkgbuild=True)
            trans_obj = get_trans_object(pkgs, repo_queue=repo_queue)
            status.transaction_queue.rpush(trans_obj.tnum)
            transaction_queue.enqueue_call(transaction_handler.handle_hook, timeout=84600)
        return json.dumps(message)


@app.route('/issues', methods=['GET'])
# @cache.memoize(timeout=900, unless=cache_buster)
def show_issues():
    return render_template('issues.html')


@app.route('/pkg/<pkgname>', methods=['GET'])
# @cache.memoize(timeout=900, unless=cache_buster)
def get_and_show_pkg_profile(pkgname=None):
    if pkgname is None or not status.all_packages.ismember(pkgname):
        abort(404)

    pkgobj = get_pkg_object(name=pkgname)
    if '' == pkgobj.description:
        desc = pkgobj.get_from_pkgbuild('pkgdesc')
        pkgobj.description = desc
        pkgobj.pkgdesc = desc

    build_history, timestamps = get_build_history_chart_data(pkgobj)

    return render_template('package.html', pkg=pkgobj, build_history=build_history,
                           timestamps=timestamps)


# @app.route('/slack/overflow', methods=['post'])
# @app.route('/slack/todo', methods=['post'])
# @app.route('/slack/tableflip', methods=['post'])
def overflow():
    res = None
    token = request.values.get('token')
    if not token or '' == token:
        abort(404)
    if 'tableflip' in request.url:
        channel = request.values.get('channel_name')
        from_user = request.values.get('user_name')
        payload = {"text": "(╯°□°)╯︵ ┻━┻", "username": from_user, "icon_emoji": ":bam:",
                   "channel": '#' + channel}
        slack = 'https://hooks.slack.com/services/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'
        requests.post(slack, data=json.dumps(payload))

        return Response(status=200)

    text = request.values.get('text')
    command = request.values.get('command')

    return Response(res['msg'], content_type=res['content_type'])


if __name__ == "__main__":
    app.run(host='127.0.0.1', port=8020, debug=True, use_reloader=False)
