#!/usr/bin/python
# -*- coding: utf-8 -*-
#
# transaction.py
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
# You should have received a copy of the GNU General Public License
# along with AntBS; If not, see <http://www.gnu.org/licenses/>.


import datetime
import os
import shutil
import subprocess
import tempfile
import time
from multiprocessing import Process

import utils.docker_util as docker_util
from utils.logging_config import logger
from utils.sign_pkgs import sign_packages
from utils.utilities import CustomSet, PacmanPackageCache, remove

from .base_objects import RedisHash, db
from .build import get_build_object
from .package import get_pkg_object
from .server_status import get_timeline_object, status
from .repo import get_repo_object

doc_util = docker_util.DockerUtils()
doc = doc_util.doc

pkg_cache_obj = PacmanPackageCache()


class TransactionMeta(RedisHash):
    """
    This is the base class for `Transaction`(s). It simply sets up the attributes
    which are stored in redis so they can be properly accessed. This class should
    not be used directly.

    Args:
        See `Transaction` docstring.

    Attributes:
        See `Transaction` docstring.
    """

    def __init__(self, packages=None, tnum=None, base_path='/var/tmp/antbs', namespace='antbs',
                 prefix='trans', repo_queue=None):
        if not any([packages, tnum]):
            raise ValueError('At least one of [packages, tnum] required.')
        elif all([packages, tnum]):
            raise ValueError('Only one of [packages, tnum] can be given, not both.')

        the_tnum = tnum
        if not tnum:
            the_tnum = self.db.incr('antbs:misc:tnum:next')

        super().__init__(namespace=namespace, prefix=prefix, key=the_tnum)

        self.key_lists.update(dict(
            string=['building', 'start_str', 'end_str'],
            bool=['is_running', 'is_finished'],
            int=['tnum'],
            list=['queue'],
            set=['packages', 'builds', 'completed', 'failed'],
            path=['base_path', 'path', 'result_dir', 'cache', 'cache_i686', 'upd_repo_result']
        ))

        self.__namespaceinit__()

        self._repo_queue = repo_queue
        self._internal_deps = []
        self._build_dirpaths = {}
        self._pkgvers = {}
        self._main_repo = get_repo_object('antergos')
        self._staging_repo = get_repo_object('antergos-staging')

        if not self or not self.tnum:
            self.__keysinit__()
            self.tnum = the_tnum
            self.base_path = base_path
            self.cache = pkg_cache_obj.cache
            self.cache_i686 = pkg_cache_obj.cache_i686

            if packages:
                for pkg in packages:
                    self.packages.add(pkg)

        for pkg in self.packages:
            self._build_dirpaths[pkg] = {'build_dir': '', '32bit': '', '32build': ''}
            self._pkgvers[pkg] = ''


class Transaction(TransactionMeta):
    """
    This class represents a single "build transaction" throughout the app. It is used
    to get/set transaction data from/to the database. A transaction is comprised
    of one or more builds. When a new transaction is initialized it creates its own build
    directory which it will delete once all builds are completed. This allows for
    build concurrency through multiple transactions and can be easily scaled as needed.

        Args:
            packages (list): Names of packages to build. This creates a new `Transaction`.
            tnum (int): Get an existing `Transaction` identified by its `tnum`.

        Attributes:
            tnum (int): This transaction's number or id if you prefer calling it that.
            base_path (str): Absolute path to the top-level build directory (for all transactions).
            path (str): Absolute path to this transaction's build directory.
            builds (list): This transaction's builds (list of bnums)
            is_running (bool): Whether or not the transaction is currently running.
            is_finished (bool): Whether or not the transaction is done (regardless of results)
            building (str): The name of the package currently building.
            start_str (str): The datetime string for when this transaction started.
            end_str (str): The datetime string for when this transaction ended.
            completed (list): Builds that completed successfully (list of bnums).
            failed (list): Builds that failed (list of bnums).
            internal_deps (list): List of packages that depend on package(s) in this transaction.

        Raises:
            ValueError: If both `packages` and `tnum` are Falsey.
    """

    def start(self):
        if self._repo_queue is None:
            logger.debug('self._repo_queue is: %s', self._repo_queue)
            raise AttributeError('_repo_queue is required to start a transaction.')

        status.current_status = 'Initializing build transaction.'
        status.transactions_running.append(self.tnum)
        self.is_running = True
        self.setup_transaction_directory()
        status.current_status = 'Processing packages.'
        self.process_packages()
        status.current_status = 'Cleaning pacman package cache.'
        PacmanPackageCache().maybe_do_cache_cleanup()

        if self.queue:
            while self.queue:
                pkg = self.queue.lpop()
                is_iso = False
                pbpath = self.get_package_build_directory(pkg)
                pkg_obj = get_pkg_object(name=pkg, pbpath=pbpath)

                for partial in ['i686', 'x86_64']:
                    if partial in pkg:
                        is_iso = True
                        pkg_obj.is_iso = True
                        break

                if is_iso:
                    result = self.build_iso(pkg_obj)
                else:
                    result = self.build_package(pkg)

                if result in [True, False]:
                    blds = pkg_obj.builds
                    total = len(blds)
                    if total > 0:
                        success = len([x for x in blds if x in status.completed])
                        failure = len([x for x in blds if x in status.failed])
                        if success > 0:
                            success = 100 * success / total
                        if failure > 0:
                            failure = 100 * failure / total

                        pkg_obj.success_rate = success
                        pkg_obj.failure_rate = failure

        self.is_running = False
        self.is_finished = True
        status.transactions_running.remove(self.tnum)

    def setup_transaction_directory(self):
        path = tempfile.mkdtemp(prefix='{0}_'.format(str(self.tnum)), dir=self.base_path)
        self.result_dir = os.path.join(path, 'result')
        self.upd_repo_result = os.path.join(path, 'upd_result')
        self.path = os.path.join(path, 'antergos-packages')

        os.mkdir(self.result_dir, mode=0o777)
        os.mkdir(self.upd_repo_result, mode=0o777)

        try:
            subprocess.check_output(['git', 'clone', status.gh_repo_url], cwd=path)
        except subprocess.CalledProcessError as err:
            raise RuntimeError(err.output)

    def get_package_build_directory(self, pkg):
        paths = [os.path.join(self.path, 'cinnamon', pkg),
                 os.path.join(self.path, pkg)]
        pbpath = None
        for p in paths:
            if os.path.exists(p):
                pbpath = p
                break
        else:
            raise RuntimeError('Unable to determine pb_path for {0}'.format(pkg))

        return pbpath

    def setup_package_build_directory(self, pkg):
        build_dir = self.get_package_build_directory(pkg)
        self._build_dirpaths[pkg].update({
            'build_dir': build_dir,
            '32bit': os.path.join(build_dir, '32bit'),
            '32build': os.path.join(build_dir, '32build')
        })
        for bdir, path in self._build_dirpaths[pkg].items():
            logger.debug(bdir)
            if not os.path.exists(path):
                os.mkdir(path, mode=0o777)

    def handle_special_cases(self, pkg, pkg_obj):
        if 'cnchi' in pkg:
            logger.info('cnchi package detected.')
            status.current_status = 'Fetching latest translations for %s from Transifex.' % pkg
            logger.info(status.current_status)
            cnchi_dir = os.path.join(self.path, pkg)
            self.fetch_and_compile_translations(translations_for=["cnchi"], pkg_obj=pkg_obj)
            remove(os.path.join(cnchi_dir, 'cnchi/.git'))
            subprocess.check_output(['tar', '-cf', 'cnchi.tar', 'cnchi'], cwd=cnchi_dir)

        elif 'numix-icon-theme-square' == pkg:
            src = os.path.join('/var/tmp/antergos-packages/', pkg, pkg + '.zip')
            dest = os.path.join('/opt/antergos-packages/', pkg)
            shutil.move(src, dest)

    def process_packages(self):

        for pkg in self.packages:
            if not pkg:
                continue

            pbpath = self.get_package_build_directory(pkg)

            pkg_obj = get_pkg_object(name=pkg, pbpath=pbpath)
            version = pkg_obj.get_version()

            if not version:
                self.packages.remove(pkg)
                logger.debug('Skipping cnchi-dev build: {0}'.format(pkg))
                continue

            pkg_obj.version_str = version
            self._pkgvers[pkg] = version

            log_msg = 'Updating pkgver in database for {0} to {1}'.format(pkg, version)
            logger.info(log_msg)
            status.current_status = log_msg

            depends = pkg_obj.get_deps()
            intersect = list(set(depends) & set(self.packages))
            if depends and len(intersect) > 0:
                self._internal_deps.append((pkg, intersect))

            self.handle_special_cases(pkg, pkg_obj)

        pkg = None
        status.current_status = 'Using package dependencies to determine build order.'
        if self._internal_deps:
            for name in self.determine_build_order(self._internal_deps):
                self.queue.append(name)

        for pkg in self.packages:
            if pkg not in self.queue:
                self.queue.append(pkg)

    def fetch_and_compile_translations(self, translations_for=None, pkg_obj=None):
        """
        Get and compile translations from Transifex.

        :param (list) translations_for:
        :param (Package) pkg_obj:

        """

        if pkg_obj is None:
            name = ''
        else:
            name = pkg_obj.name

        trans = {
            "cnchi": {
                'trans_dir': "/opt/cnchi-translations/",
                'trans_files_dir': '/opt/cnchi-translations/translations/antergos.cnchi',
                'dest_dir': os.path.join(self.path, name, '/cnchi/po')
            },
            "cnchi_updater": {
                'trans_dir': "/opt/antergos-iso-translations/",
                'trans_files_dir': "/opt/antergos-iso-translations/translations/antergos.cnchi_updaterpot",
                'dest_dir': '/srv/antergos.info/repo/iso/testing/trans/cnchi_updater'
            },
            "antergos-gfxboot": {
                'trans_dir': "/opt/antergos-iso-translations/",
                'trans_files_dir': '/opt/antergos-iso-translations/translations/antergos.antergos-gfxboot',
                'dest_dir': '/srv/antergos.info/repo/iso/testing/trans/antergos-gfxboot'
            }
        }

        for trans_for in translations_for:

            if not os.path.exists(trans[trans_for]['dest_dir']):
                os.mkdir(trans[trans_for]['dest_dir'])
            try:

                output = subprocess.check_output(['tx', 'pull', '-a', '--minimum-perc=50'],
                                                 cwd=trans[trans_for]['trans_dir'])

                for r, d, f in os.walk(trans[trans_for]['trans_files_dir']):
                    for tfile in f:
                        if trans_for in ['cnchi', 'antergos-gfxboot']:
                            tfile = os.path.join(r, tfile)
                            logger.debug(
                                'Copying %s to %s' % (tfile, trans[trans_for]['dest_dir']))
                            shutil.copy(tfile, trans[trans_for]['dest_dir'])
                        elif 'cnchi_updater' == trans_for:
                            mofile = tfile[:-2] + 'mo'
                            subprocess.check_call(['msgfmt', '-v', tfile, '-o', mofile],
                                                  cwd=trans[trans_for]['trans_files_dir'])
                            os.rename(os.path.join(trans[trans_for]['trans_files_dir'], mofile),
                                      os.path.join(trans[trans_for]['dest_dir'], mofile))

            except subprocess.CalledProcessError as err:
                logger.error(err.output)
            except Exception as err:
                logger.error(err)

    @staticmethod
    def determine_build_order(source):
        """
        Performs a topological sort on elements. This determines the order in which
        packages must be built based on internal (to this transaction) dependencies.

        Args:
            source (list): A list of ``(name, [list of dependancies])`` pairs.

        Returns:
            A list of names, with dependancies listed first.

        Raises:
            ValueError: When cyclic or missing dependancy detected.

        """
        # copy deps so we can modify set in-place
        pending = [(name, set(deps)) for name, deps in source]
        emitted = []
        try:
            while pending:
                next_pending = []
                next_emitted = []

                for entry in pending:
                    name, deps = entry
                    # remove deps we emitted last pass
                    deps.difference_update(emitted)

                    if deps:
                        # still has deps? recheck during next pass
                        next_pending.append(entry)
                    else:
                        # no more deps? time to emit
                        yield name
                        emitted.append(name)
                        # remember what we emitted for difference_update() in next pass
                        next_emitted.append(name)

                if not next_emitted:
                    # all entries have unmet deps, one of two things is wrong...
                    logger.error("cyclic or missing dependancy detected: %r", next_pending)
                    raise ValueError
                pending = next_pending
                emitted = next_emitted
        except ValueError as err:
            logger.error(err)

    def process_and_save_build_metadata(self, pkg_obj, version_str=None):
        """
        Creates a new build for a package, initializes the build data, and returns a build object.

        Args:
            pkg_obj (Package): Package object for the package being built.

        Returns:
            Build: A build object.

        """

        bld_obj = get_build_object(pkg_obj=pkg_obj, tnum=self.tnum)
        bld_obj.start_str = datetime.datetime.now().strftime("%m/%d/%Y %I:%M%p")
        bld_obj.version_str = version_str if version_str else pkg_obj.version_str

        tpl = 'Build <a href="/build/{0}">{0}</a> for <strong>{1}-{2}</strong> started.'
        tlmsg = tpl.format(bld_obj.bnum, pkg_obj.name, version_str)

        get_timeline_object(msg=tlmsg, tl_type=3, ret=False)

        pkg_obj.builds.append(bld_obj.bnum)
        self.builds.add(bld_obj.bnum)
        status.now_building.append(bld_obj.bnum)

        return bld_obj

    def build_package(self, pkg):

        pbpath = self.get_package_build_directory(pkg)
        pkg_obj = get_pkg_object(name=pkg, pbpath=pbpath)
        self.building = pkg
        status.current_status = 'Building {0}-{1} with makepkg.'.format(pkg, pkg_obj.version_str)

        in_dir_last = len([name for name in os.listdir(self.result_dir)])
        db.setex('antbs:misc:pkg_count:{0}'.format(self.tnum), 86400, in_dir_last)

        bld_obj = self.process_and_save_build_metadata(pkg_obj, self._pkgvers[pkg])

        doc_util.do_docker_clean(pkg_obj.name)
        self.setup_package_build_directory(pkg)

        build_env = ['_AUTOSUMS=True'] if pkg_obj.autosum else ['_AUTOSUMS=False']

        if '/cinnamon/' in pkg_obj.pbpath:
            build_env.append('_ALEXPKG=True')
        else:
            build_env.append('_ALEXPKG=False')

        build_dir = self._build_dirpaths[pkg]['build_dir']
        _32bit = self._build_dirpaths[pkg]['32bit']
        _32build = self._build_dirpaths[pkg]['32build']
        hconfig = doc_util.get_host_config('packages', build_dir, self.result_dir, self.cache,
                                           self.cache_i686, _32build, _32bit)
        container = {}
        try:
            container = doc.create_container("antergos/makepkg",
                                             command='/makepkg/build.sh',
                                             volumes=['/var/cache/pacman', '/makepkg', '/antergos',
                                                      '/pkg', '/root/.gnupg', '/staging', '/32bit',
                                                      '/32build', '/result',
                                                      '/var/cache/pacman_i686'],
                                             environment=build_env, cpuset='0-3',
                                             name=pkg_obj.name, host_config=hconfig)
            if container.get('Warnings', False):
                logger.error(container.get('Warnings'))
        except Exception as err:
            logger.error('Create container failed. Error Msg: %s', err)
            bld_obj.failed = True

        cont = container.get('Id', '')
        bld_obj.container = cont
        status.container = cont
        stream_process = Process(target=bld_obj.publish_build_output, kwargs=dict(upd_repo=False))

        try:
            doc.start(cont)
            stream_process.start()
            result = doc.wait(cont)
            if int(result) != 0:
                bld_obj.failed = True
                tpl = 'Container %s exited with a non-zero return code. Return code was %s'
                logger.error(tpl, pkg_obj.name, result)
            else:
                bld_obj.completed = True
                logger.info('Container %s exited. Return code was %s', pkg_obj.name, result)
        except Exception as err:
            bld_obj.failed = True
            logger.error('Start container failed. Error Msg: %s', err)

        stream_process.join()

        packages_signed = False
        if bld_obj.completed:
            logger.debug('bld_obj.completed!')
            if sign_packages(bld_obj.pkgname):
                packages_signed = True

        if packages_signed:
            tpl = 'Build <a href="/build/{0}">{0}</a> for <strong>{1}-{2}</strong> was successful.'
            tlmsg = tpl.format(str(bld_obj.bnum), pkg_obj.name, bld_obj.version_str)
            _ = get_timeline_object(msg=tlmsg, tl_type=4)
            status.completed.rpush(bld_obj.bnum)
            bld_obj.review_status = 'pending'
            self._staging_repo.update_repo(bld_obj=bld_obj)
        else:
            tpl = 'Build <a href="/build/{0}">{0}</a> for <strong>{1}-{2}</strong> failed.'
            tlmsg = tpl.format(str(bld_obj.bnum), pkg_obj.name, bld_obj.version_str)
            _ = get_timeline_object(msg=tlmsg, tl_type=5)
            bld_obj.failed = True
            bld_obj.completed = False

        bld_obj.end_str = datetime.datetime.now().strftime("%m/%d/%Y %I:%M%p")

        self.building = ''
        status.now_building.remove(bld_obj.bnum)
        if not bld_obj.failed:
            pkg_obj = get_pkg_object(bld_obj.pkgname)
            last_build = pkg_obj.builds[-2] if pkg_obj.builds else None
            if last_build:
                last_bld_obj = get_build_object(bnum=last_build)
                if 'pending' == last_bld_obj.review_status and last_bld_obj.bnum != bld_obj.bnum:
                    last_bld_obj.review_status = 'skip'

            return True

        status.failed.rpush(bld_obj.bnum)
        return False

    def build_iso(self, pkg_obj=None):
        # TODO: Rework this, possibly abstract away parts in common with self.build_package()

        status.iso_building = True

        bld_obj = self.process_and_save_build_metadata(pkg_obj=pkg_obj)
        build_id = bld_obj.bnum

        self.fetch_and_compile_translations(translations_for=["cnchi_updater", "antergos-gfxboot"])

        flag = '/srv/antergos.info/repo/iso/testing/.ISO32'
        minimal = '/srv/antergos.info/repo/iso/testing/.MINIMAL'

        if 'i686' in pkg_obj.name:
            if not os.path.exists(flag):
                open(flag, 'a').close()
        else:
            if os.path.exists(flag):
                os.remove(flag)

        if 'minimal' in pkg_obj.name:
            out_dir = '/out'
            if not os.path.exists(minimal):
                open(minimal, 'a').close()
        else:
            out_dir = '/out'
            if os.path.exists(minimal):
                os.remove(minimal)

        in_dir_last = len([name for name in os.listdir('/srv/antergos.info/repo/iso/testing')])
        db.set('pkg_count_iso', in_dir_last)

        # Create docker host config dict
        hconfig = doc.create_host_config(privileged=True, cap_add=['ALL'],
                                         binds={
                                             '/opt/archlinux-mkarchiso':
                                                 {
                                                     'bind': '/start',
                                                     'ro': False
                                                 },
                                             '/run/dbus':
                                                 {
                                                     'bind': '/var/run/dbus',
                                                     'ro': False
                                                 },
                                             '/srv/antergos.info/repo/iso/testing':
                                                 {
                                                     'bind': out_dir,
                                                     'ro': False
                                                 }},
                                         restart_policy={
                                             "MaximumRetryCount": 2,
                                             "Name": "on-failure"},
                                         mem_limit='2G',
                                         memswap_limit='-1')
        iso_container = {}
        try:
            iso_container = doc.create_container("antergos/mkarchiso", command='/start/run.sh',
                                                 name=pkg_obj.name, host_config=hconfig,
                                                 cpuset='0-3')
            if iso_container.get('Warnings', False) and iso_container.get('Warnings') != '':
                logger.error(iso_container.get('Warnings'))
        except Exception as err:
            logger.error('Create container failed. Error Msg: %s' % err)
            bld_obj.failed = True
            return False

        bld_obj.container = iso_container.get('Id')
        status.container = bld_obj.container
        open('/opt/archlinux-mkarchiso/first-run', 'a').close()

        try:
            doc.start(bld_obj.container)
            cont = bld_obj.container
            stream_process = Process(target=self.publish_build_output,
                                     kwargs=dict(container=cont, bld_obj=bld_obj, is_iso=True,
                                                 tnum=self.tnum))
            stream_process.start()
            result = doc.wait(cont)
            inspect = doc.inspect_container(cont)
            if result != 0:
                if inspect['State'].get('Restarting', '') or inspect.get('RestartCount', 0) != 2:
                    while inspect['State'].get('Restarting', '') or inspect.get('RestartCount', 0) != 2:
                        time.sleep(5)
                        inspect = doc.inspect_container(cont)

            if inspect['State'].get('ExitCode', 1) == 1:
                bld_obj.failed = True
                logger.error('[CONTAINER EXIT CODE] Container %s exited. Return code was %s',
                             pkg_obj.name, result)
            else:
                bld_obj.completed = True
                logger.info('[CONTAINER EXIT CODE] Container %s exited. Return code was %s',
                            pkg_obj.name, result)
        except Exception as err:
            logger.error('Start container failed. Error Msg: %s', err)
            bld_obj.failed = True
            return False

        stream_process.join()
        in_dir = len([name for name in os.listdir('/srv/antergos.info/repo/iso/testing')])
        last_count = int(db.get('pkg_count_iso'))

        if in_dir > last_count:
            bld_obj.completed = True
            tpl = 'Build <a href="/build/{0}">{0}</a> for <strong>{1}</strong> was successful.'
            tlmsg = tpl.format(build_id, pkg_obj.name)
            _ = get_timeline_object(msg=tlmsg, tl_type=4)
            status.completed.rpush(bld_obj.bnum)
        else:
            bld_obj.failed = True
            bld_obj.completed = False
            tpl = 'Build <a href="/build/{0}">{0}</a> for <strong>{1}</strong> failed.'
            tlmsg = tpl.format(build_id, pkg_obj.name)
            _ = get_timeline_object(msg=tlmsg, tl_type=5)
            status.failed.rpush(build_id)

        bld_obj.end_str = datetime.datetime.now().strftime("%m/%d/%Y %I:%M%p")

        if not bld_obj.failed:
            remove('/opt/archlinux-mkarchiso/antergos-iso')
            doc_util.do_docker_clean(pkg_obj.name)
            return True

        return False


def get_trans_object(packages=None, tnum=None, repo_queue=None):
    """
    Gets an existing transaction or creates a new one.

    Args:
        packages (list): Create a new transaction with these packages.
        tnum (int): Get an existing transaction identified by `tnum`.

    Returns:
        Transaction: A fully initiallized `Transaction` object.

    Raises:
        ValueError: If both `packages` and `tnum` are Falsey or Truthy.

    """
    if not any([packages, tnum]):
        raise ValueError('At least one of [packages, tnum] required.')
    elif all([packages, tnum]):
        raise ValueError('Only one of [packages, tnum] can be given, not both.')

    trans_obj = Transaction(packages=packages, tnum=tnum, repo_queue=repo_queue)

    return trans_obj