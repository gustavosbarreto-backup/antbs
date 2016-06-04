#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
#  repo.py
#
#  Copyright © 2015-2016 Antergos
#
#  This file is part of The Antergos Build Server, (AntBS).
#
#  AntBS is free software; you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation; either version 3 of the License, or
#  (at your option) any later version.
#
#  AntBS is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  The following additional terms are in effect as per Section 7 of the license:
#
#  The preservation of all legal notices and author attributions in
#  the material or in the Appropriate Legal Notices displayed
#  by works containing it is required.
#
#  You should have received a copy of the GNU General Public License
#  along with AntBS; If not, see <http://www.gnu.org/licenses/>.


import os
import tarfile
import gevent
from multiprocessing import Process

import re

from database.base_objects import RedisHash
from database.server_status import status
from utils.logging_config import logger
from utils.utilities import Singleton, remove
import utils.docker_util as docker_util

doc_util = docker_util.DockerUtils()
doc = doc_util.doc


class PacmanRepo(RedisHash):
    """
    This class represents a "repo" throughout this application. It is used to
    get/set metadata about the repos that this application manages from/to the database.

    Args:
        name (str): The name of the repo (as it would be configured in pacman.conf).
        path (str): The absolute path to the repo's directory on the server.

    Attributes:
        (str)
            name: see args description above.
            path: see args description above.

        (bool)
            n/a

        (int)
            pkg_count_alpm: Total number of packages in the repo (as per alpm database).
            pkg_count_fs: Total number of packages in the repo (files found on server).

        (list)
            n/a

        (set)
            pkgs_fs: List of the package files in the repo's directory on the server (pkg names)
            pkgs_alpm: List of packages that are in the repo's alpm database file (this is what pacman sees).

    """

    def __init__(self, name=None, path=None, prefix='repo'):
        if not name:
            raise RuntimeError

        super().__init__(prefix=prefix, key=name)

        self.attrib_lists.update(
            dict(string=['name'],
                 bool=['locked'],
                 int=['pkg_count_alpm', 'pkg_count_fs'],
                 list=[],
                 set=['pkgs_fs', 'pkgs_alpm', 'packages', 'unaccounted_for'],
                 path=['path']))

        super().__namespaceinit__()

        if not self or not self.name:
            self.__keysinit__()
            self.name = name
            self.path = os.path.join(path, name)
            status.repos.add(name)

        self.save_repo_state_alpm()
        self.save_repo_state_filesystem()
        self.setup_packages_manifest()
        self.get_repo_packages_unaccounted_for()

    def setup_packages_manifest(self):
        pkgs_fs = set([p.split('|')[0] for p in self.pkgs_fs if p])
        pkgs_alpm = set([p.split('|')[0] for p in self.pkgs_alpm if p])
        pkgs = list(pkgs_fs & pkgs_alpm)
        unaccounted_for = [p.split('|')[0] for p in list(pkgs_fs) + list(pkgs_alpm) if p not in pkgs]

        for pk in self.packages:
            if pk not in pkgs:
                self.packages.remove(pk)

        for pkg in pkgs:
            self.packages.add(pkg)

        for pk in self.unaccounted_for:
            if pk in pkgs:
                self.unaccounted_for.remove(pk)

        for pak in unaccounted_for:
            self.unaccounted_for.add(pak)

    def _pkgname_matches(self, pkgname, match_in):
        pattern = r'{}\|'.format(pkgname)
        return re.match(pattern, match_in)

    def _get_pkgnames(self, location):
        return [p.split('|')[0] for p in location if p]

    def get_pkgnames_filesystem(self):
        return self._get_pkgnames(self.pkgs_fs)

    def get_pkgnames_alpm(self):
        return self._get_pkgnames(self.pkgs_alpm)

    def _get_pkgver(self, pkgname, location):
        pkgs = self._get_pkgnames(location)

        if pkgname not in pkgs:
            return ''

        pkgver = [p.split('|')[1] for p in location if p and self._pkgname_matches(pkgname, p)]

        logger.debug(pkgver)

        return pkgver[0] or ''

    def get_pkgver_alpm(self, pkgname):
        return self._get_pkgver(pkgname, self.pkgs_alpm)

    def get_pkgver_filesystem(self, pkgname):
        return self._get_pkgver(pkgname, self.pkgs_fs)

    def _has_package(self, pkgname, location):
        return pkgname in self._get_pkgnames(location)

    def has_package_filesystem(self, pkgname):
        return self._has_package(pkgname, self.pkgs_fs)

    def has_package_alpm(self, pkgname):
        return self._has_package(pkgname, self.pkgs_alpm)

    def save_repo_state_filesystem(self):
        repodir = os.path.join(self.path, 'x86_64')
        pkgs = set(p for p in os.listdir(repodir) if '.pkg.' in p and not p.endswith('.sig'))

        for pkg in pkgs:
            pkg = os.path.basename(pkg)
            try:
                pkg, version, rel, suffix = pkg.rsplit('-', 3)
            except ValueError:
                logger.error("unexpected pkg: " + pkg)
                continue

            self.pkgs_fs.add('{0}|{1}-{2}'.format(pkg, version, rel))

        self.pkg_count_fs = len(self.pkgs_fs)

    def save_repo_state_alpm(self):
        repodir = os.path.join(self.path, 'x86_64')
        dbfile = os.path.join(repodir, '%s.db.tar.gz' % self.name)

        with tarfile.open(dbfile, 'r') as pacman_db:
            for pkg in pacman_db.getnames():
                pkg = pkg.split('/', 1)[0]
                pkgname, ver, rel = pkg.rsplit('-', 2)

                self.pkgs_alpm.add('{0}|{1}-{2}'.format(pkgname, ver, rel))

        self.pkg_count_alpm = len(self.pkgs_alpm)

    def get_repo_packages_unaccounted_for(self):
        unaccounted_for = []

        if self.unaccounted_for:
            for pkg in self.unaccounted_for:
                _pkg = dict(pkgname=pkg, fs=None, alpm=None)

                if self.has_package_filesystem(pkg):
                    _pkg['fs'] = self.get_pkgver_filesystem(pkg)

                if self.has_package_alpm(pkg):
                    _pkg['alpm'] = self.get_pkgver_alpm(pkg)

                unaccounted_for.append(_pkg)

        return unaccounted_for

    def update_repo(self, review_result=False,
                    result_dir='/tmp/update_repo_result', pkgs2_add_rm=None):

        repodir = 'staging' if 'staging' in self.name else 'main'
        trans_running = status.transactions_running or status.transaction_queue
        building_saved = False
        excluded = ['Updating antergos repo database.',
                    'Updating antergos-staging repo database.',
                    'Processing developer review result.']

        if not status.idle and trans_running and status.current_status not in excluded:
            building_saved = status.current_status
        elif status.idle:
            status.idle = False

        status.current_status = excluded[0] if 'main' == repodir else excluded[1]

        if os.path.exists(result_dir):
            remove(result_dir)

        os.mkdir(result_dir, 0o777)

        command = ['/makepkg/build.sh']
        if pkgs2_add_rm:
            command.extend(pkgs2_add_rm)

        pkgenv = ['_PKGNAME={0}'.format(bld_obj.pkgname),
                  '_PKGVER={0}'.format(bld_obj.pkgver),
                  '_RESULT={0}'.format(review_result),
                  '_UPDREPO=True',
                  '_REPO={0}'.format(self.name),
                  '_REPO_DIR={0}'.format(repodir)]

        doc_util.do_docker_clean("update_repo")

        hconfig = doc_util.get_host_config('repo_update', result_dir)
        volumes = ['/makepkg', '/root/.gnupg', '/main', '/result', '/staging']

        try:
            container = doc.create_container("antergos/makepkg", command=command,
                                             name="update_repo", environment=pkgenv,
                                             volumes=volumes, host_config=hconfig)

            cont = container.get('Id')
            bld_obj.repo_container = cont
            doc.start(cont)
            if not review_result:
                stream_process = Process(target=bld_obj.publish_build_output,
                                         kwargs=dict(upd_repo=True))
                stream_process.start()

            result = doc.wait(cont)
            if not review_result:
                stream_process.join()

            if int(result) != 0:
                logger.error('update repo failed. exit status is: %s', result)
            else:
                # doc.remove_container(container, v=True)
                pass

        except Exception as err:
            result = 1
            logger.error('Start container failed. Error Msg: %s' % err)

        if not status.idle:
            if building_saved:
                status.current_status = building_saved
            elif not status.transactions_running and not status.now_building:
                status.idle = True
                status.current_status = 'Idle.'

        return result == 0


class AntergosRepo(PacmanRepo, metaclass=Singleton):
    def __init__(self, name='antergos', *args, **kwargs):
        super().__init__(name=name, *args, **kwargs)


class AntergosStagingRepo(PacmanRepo, metaclass=Singleton):
    def __init__(self, name='antergos-staging', *args, **kwargs):
        super().__init__(name=name, *args, **kwargs)


def get_repo_object(name, path=None):
    if not path:
        path = status.REPO_BASE_DIR
    if 'antergos' == name:
        repo = AntergosRepo(name=name, path=path)
    elif 'antergos-staging' == name:
        repo = AntergosStagingRepo(name=name, path=path)
    else:
        raise TypeError('name must be one of [antergos, antergos-staging]')

    return repo
