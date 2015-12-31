#!/usr/bin/python
# -*- coding: utf-8 -*-
#
# webhook.py
#
# Copyright © 2013-2015 Antergos
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


"""Webhook Handler Module"""

import json
import os
import subprocess
import shutil
import datetime
import ast

from rq import Queue, Connection, Worker
import ipaddress
import requests

import build_pkg as builder
from utils.redis_connection import db
import utils.logging_config as logconf
import package as package
from utils.server_status import status, TimelineEvent

logger = logconf.logger
empty_dict = dict()

with Connection(db):
    queue = Queue('hook_queue')
    w = Worker([queue])


def rm_file_or_dir(src):
    """

    :param src:
    :return:
    """
    if os.path.isdir(src):
        try:
            shutil.rmtree(src)
        except Exception as err:
            logger.error(err)
            return True
    elif os.path.isfile(src):
        try:
            os.remove(src)
        except Exception as err:
            logger.error(err)
            return True
    else:
        return True


class Webhook(object):
    """
    This class handles the processing of all Webhooks.

    Args:
        request (dict): The request dict (from Flask)

    Attributes:
       can_process (bool): The request is from an authorized sender.
       is_monitor (bool): The request is internel (repo_monitor.py).
       is_cnchi (bool): The request is from Cnchi Installer.
       request (dict): The Flask request.
       is_manual (bool): The request was made by a human.
       manual_trans_index (int): The db list index of the github trans that is to be processed.
       is_numix (bool): The request is for a numix icon repo.
       is_github (bool): The request is from Github.
       is_gitlab (bool): The request is from Gitlab.
       changes (list): Packages for which changes were made to their PKGBUILD in git commit.
       repo (string): The name of the github repo that triggered the webhook.
       payload (dict): The webhook data payload.
       full_name (string): The github repo's full name. eg. "org/repo"
       pusher (string): The name of the pusher.
       commits (list): List of commits in the payload.
       result (string): Result string to send as response to the request.
       building (string): The name of the package being built if a build is running currently.

    """
    def __init__(self, request=empty_dict):
        self.can_process = False
        self.is_monitor = False
        self.is_cnchi = False
        try:
            self.request = request.method
        except (AttributeError, KeyError):
            self.request = False
            self.is_monitor = True
        if self.request is False or db is None or queue is None:
            logger.error('Cant process webhook because request or db or queue is None.')
        elif self.request or self.is_monitor:
            self.can_process = True
            self.request = request
            self.is_manual = False
            self.manual_trans_index = None
            self.is_numix = False
            self.is_github = False
            self.is_gitlab = False
            self.changes = []
            self.repo = 'antergos-packages'
            self.payload = None
            self.full_name = None
            self.pusher = None
            self.commits = None
            self.result = None
            self.building = status.now_building
            self.result = None

            self.is_authorized = self.is_from_authorized_sender()

            if self.is_authorized:
                # Process Webhook
                if self.is_manual:
                    self.process_manual()

                if self.is_cnchi and not self.request.args.get('result', False):
                    self.process_cnchi_start()
                elif self.is_cnchi and self.request.args.get('result', False):
                    install_id = self.request.args.get('install_id', None)
                    result = self.request.args.get('result', None)
                    if install_id and result:
                        self.process_cnchi_end(install_id, result)

                if self.is_github:
                    self.process_github()
                if len(self.changes) > 0:
                    self.process_changes()
            else:
                if self.result is None:
                    self.result = 'Nothing to see here, move along ...'

    def is_from_authorized_sender(self):
        """
        Determine if the request sender is authorized to send us webhooks.

        Returns:
            bool: The request is from an authorized sender.

        """
        if self.is_monitor is True:
            return True
        manual = int(self.request.args.get('phab', '0'))
        gitlab = self.request.headers.get('X-Gitlab-Event') or ''
        cnchi = self.request.args.get('cnchi', False)
        cnchi_version = self.request.headers.get('X-Cnchi-Installer', False)
        if manual and manual > 0 and self.request.args.get('token') == db.get('ANTBS_MANUAL_TOKEN'):
            self.is_manual = True
            self.manual_trans_index = int(manual)
        elif cnchi and cnchi == db.get('CNCHI_TOKEN_NEW') and cnchi_version:
            self.is_cnchi = cnchi_version
        elif '' != gitlab and 'Push Hook' == gitlab:
            self.is_gitlab = True
            self.repo = 'antergos-packages'
            self.full_name = 'Antergos/antergos-packages'
            self.changes = [['numix-icon-theme-square', 'numix-icon-theme-square-kde']]
        else:
            if not db.exists('GITHUB_HOOK_IP_BLOCKS'):
                # Store the IP address blocks that github uses for hook requests.
                hook_blocks = requests.get('https://api.github.com/meta').text
                db.setex('GITHUB_HOOK_IP_BLOCKS', 42300, hook_blocks)
                hook_blocks = json.loads(hook_blocks)['hooks']
            else:
                hook_blocks = json.loads(db.get('GITHUB_HOOK_IP_BLOCKS'))['hooks']
            for block in hook_blocks:
                ip = ipaddress.ip_address(u'%s' % self.request.remote_addr)
                if ipaddress.ip_address(ip) in ipaddress.ip_network(block):
                    # the remote_addr is within the network range of github
                    self.is_github = True
                    break
            else:
                return False

            if self.request.headers.get('X-GitHub-Event') == "ping":
                self.result = json.dumps({'msg': 'Hi!'})
                return False
            elif self.request.headers.get('X-GitHub-Event') != "push":
                self.result = json.dumps({'msg': "wrong event type"})
                return False

        return True

    def process_manual(self):
        index = self.manual_trans_index
        try:
            key = db.lrange('antbs:github:payloads:index', -index, -index)
            logger.info(key)
            logger.info(key[0])
            self.payload = db.hgetall(key[0])
        except Exception as err:
            logger.error(err)
            self.result = 500
            return
        self.commits = ast.literal_eval(self.payload['commits'])
        self.is_github = True
        self.full_name = 'Antergos/antergos-packages'
        self.repo = 'antergos-packages'

    def process_github(self):
        if not self.is_manual:
            self.payload = json.loads(self.request.data)
            # Save payload in the database temporarily in case we need it later.
            dt = datetime.datetime.now().strftime("%m%d%Y-%I%M")
            key = 'antbs:github:payloads:%s' % dt
            if db.exists(key):
                for i in range(1, 5):
                    tmp = '%s:%s' % (key, i)
                    if not db.exists(tmp):
                        key = tmp
                        break
            db.hmset(key, self.payload)
            db.rpush('antbs:github:payloads:index', key)
            db.expire(key, 172800)

            self.full_name = self.payload['repository']['full_name']
            self.repo = self.payload['repository']['name']
            self.pusher = self.payload['pusher']['name']
            self.commits = self.payload['commits']

        if self.repo == 'numix-icon-theme':
            rate_limit = True
            if 'numix-icon-theme' not in status.queue and 'numix-icon-theme' != status.building:
                if not db.exists('numix-commit-flag'):
                    self.changes.append(['numix-icon-theme'])
                    self.is_numix = True
                    db.setex('numix-commit-flag', 3600, 'True')
                    rate_limit = False

            if rate_limit:
                msg = 'RATE LIMIT IN EFFECT FOR numix-icon-theme'
                logger.info(msg)
                self.result = json.dumps({'msg': msg})
            else:
                self.repo = 'antergos-packages'

        elif self.repo == 'cnchi-dev':
            self.changes.append(['cnchi-dev'])
            self.repo = 'antergos-packages'
            self.is_cnchi = True

        elif self.pusher != "antbs":
            for commit in self.commits:
                self.changes.append(commit['modified'])
                self.changes.append(commit['added'])

    def process_changes(self):
        if self.repo == "antergos-packages":
            logger.info("Build hook triggered. Updating build queue.")
            has_pkgs = False
            no_dups = []

            for changed in self.changes:
                # logger.info(changed)
                if len(changed) > 0:
                    for item in changed:
                        # logger.info(item)
                        if self.is_gitlab or self.is_numix or self.is_cnchi:
                            pak = item
                        else:
                            if "PKGBUILD" in item:
                                pak, pkb = item.rsplit('/', 1)
                                pak = pak.rsplit('/', 1)[-1]
                            else:
                                pak = None

                        # logger.info(pak)
                        if pak and pak != 'antergos-iso':
                            logger.info('Adding %s to the build queue' % pak)
                            no_dups.append(pak)
                            status.all_packages.add(pak)
                            has_pkgs = True

            if has_pkgs:
                the_pkgs = list(set(no_dups))
                first = True
                last = False
                last_pkg = the_pkgs[-1]
                html = []
                if len(the_pkgs) > 1:
                    html.append('<ul class="hook-pkg-list">')
                for p in the_pkgs:
                    if p and p in status.hook_queue:
                        continue
                    if p and p not in status.hook_queue:
                        status.hook_queue.rpush(p)
                        if len(the_pkgs) > 1:
                            item = '<li>%s</li>' % p
                        else:
                            item = '<strong>%s</strong>' % p
                        html.append(item)
                    if p == last_pkg:
                        if self.is_gitlab:
                            source = 'Gitlab'
                            tltype = 2
                        else:
                            source = 'Github'
                            tltype = 1
                        if len(the_pkgs) > 1:
                            html.append('</ul>')
                        the_pkgs_str = ''.join(html)
                        tl_event = TimelineEvent(
                            msg='Webhook triggered by <strong>%s.</strong> Packages added to the build queue: %s' % (
                                source, the_pkgs_str), tl_type=tltype, packages=the_pkgs)
                        p_obj = package.Package(p)
                        events = p_obj.tl_events
                        events.append(tl_event.event_id)
                        del [p_obj, events]
                    first = False

                queue.enqueue_call(builder.handle_hook, timeout=84600)

            if not self.result:
                self.result = json.dumps({'msg': 'OK!'})

    def process_cnchi_start(self):
        """
        Generate installation ID then store it along with the clients ip in result variable.

        :return: None
        """

        install_id = str(db.incr('cnchi:install_id:next'))
        client_ip = self.request.remote_addr
        user_hash_key = 'cnchi:user:%s' % client_ip
        install_hash_key = 'cnchi:install:%s' % install_id
        dt = datetime.datetime.now().strftime("%m/%d/%Y %I:%M%p")
        db.hsetnx(user_hash_key, 'ip', client_ip)
        db.hsetnx(user_hash_key, install_id + ':start', dt)
        db.hsetnx(user_hash_key, install_id + ':cnchi', str(self.is_cnchi))
        install_hash = {'id': install_id,
                        'ip': client_ip,
                        'start': dt,
                        'cnchi_version': self.is_cnchi,
                        'successful': "False"}
        db.hmset(install_hash_key, install_hash)

        self.result = json.dumps({'id': install_id, 'ip': client_ip})

    def process_cnchi_end(self, install_id, result):
        """
            Record install result (success/failure).

            :return: None
        """
        install_hash_key = 'cnchi:install:%s' % install_id
        client_ip = self.request.remote_addr
        user_hash_key = 'cnchi:user:%s' % client_ip
        dt = datetime.datetime.now().strftime("%m/%d/%Y %I:%M%p")
        db.hsetnx(user_hash_key, str(install_id) + ':end', dt)
        db.hsetnx(user_hash_key, str(install_id) + ':successful', result)
        db.hset(install_hash_key, 'successful', result)
        db.hset(install_hash_key, 'end', dt)

        self.result = json.dumps({'msg': 'Ok!'})


