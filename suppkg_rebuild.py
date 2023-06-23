#!/usr/bin/python3

import argparse
import logging
import sys
from urllib.error import HTTPError

import re
from lxml import etree as ET
from collections import defaultdict

import osc.conf
import osc.core

from osc.util.helper import decode_list
from osclib.conf import Config
from osclib.stagingapi import StagingAPI

OPENSUSE = 'openSUSE:Factory'

makeurl = osc.core.makeurl
http_GET = osc.core.http_GET
http_POST = osc.core.http_POST
http_PUT = osc.core.http_PUT


class StagingHelper(object):
    def __init__(self, project):
        self.project = project
        self.apiurl = osc.conf.config['apiurl']
        Config(self.apiurl, self.project)
        self.api = StagingAPI(self.apiurl, self.project)

    def get_support_package_list(self, project, repository):
        f = decode_list(osc.core.get_buildconfig(self.apiurl, project, repository).splitlines())
        pkg_list = []
        for line in f:
            if re.match('Preinstall', line) or re.match('VM[Ii]nstall', line) or re.match('Support', line):
                content = line.split(':')
                variables = [x.strip() for x in content[1].split(' ')]
                for var in variables:
                    if var != '' and var not in pkg_list:
                        if var.startswith('!') and var[1:] in pkg_list:
                            pkg_list.remove(var[1:])
                        else:
                            pkg_list.append(var)
        return pkg_list

    def get_project_binarylist(self, project, repository, arch):
        query = {'view': 'binaryversions'}
        return ET.parse(
            http_GET(
                makeurl(
                    self.apiurl, ['build', project, repository, arch], query=query
                )
            )
        ).getroot()

    def process_project_binarylist(self, project, repository, arch):
        prj_binarylist = self.get_project_binarylist(project, repository, arch)
        files = {}
        for package in prj_binarylist.findall('./binaryversionlist'):
            for binary in package.findall('binary'):
                result = re.match(r'(.*)-([^-]*)-([^-]*)\.([^-\.]+)\.rpm', binary.attrib['name'])
                if not result:
                    continue
                bname = result[1]
                if bname.endswith('-debuginfo') or bname.endswith('-debuginfo-32bit'):
                    continue
                if bname.endswith('-debugsource'):
                    continue
                if bname.startswith('::import::'):
                    continue
                if result[4] == 'src':
                    continue
                files[bname] = package.attrib['package'].split(':', 1)[0]

        return files

    def check_multiple_specs(self, project, packages):
        expanded_packages = []

        for pkg in packages:
            query = {'expand': 1}
            url = makeurl(self.apiurl, ['source', project, pkg], query=query)
            try:
                root = ET.parse(http_GET(url)).getroot()
            except HTTPError as e:
                if e.code == 404:
                    continue
                raise
            expanded_packages.extend(
                en.attrib['name'][:-5]
                for en in root.findall('entry')
                if en.attrib['name'].endswith('.spec')
            )
        return expanded_packages

    def crawl(self):
        """Main method"""
        rebuild_data = self.api.pseudometa_file_load('support_pkg_rebuild')
        if rebuild_data is None:
            print("There is no support_pkg_rebuild file!")
            return

        logging.info(f'Gathering support package list from {self.project}')
        support_pkgs = self.get_support_package_list(self.project, 'standard')
        files = self.process_project_binarylist(self.project, 'standard', 'x86_64')
        staging_projects = [
            f"{self.api.cstaging}:{p}"
            for p in self.api.get_staging_projects_short()
        ]
        cand_sources = defaultdict(list)
        for stg in staging_projects:
            status = self.api.project_status(stg, status=False)
            prj_staged_packages = [req.get('package') for req in status.findall('./staged_requests/request')]
            prj_expanded_packages = self.check_multiple_specs(self.project, prj_staged_packages)
            for pkg in support_pkgs:
                if files.get(pkg) and files.get(pkg) in prj_expanded_packages:
                    if files.get(pkg) not in cand_sources[stg]:
                        cand_sources[stg].append(files.get(pkg))

        root = ET.fromstring(rebuild_data)

        logging.info('Checking rebuild data...')

        for stg in root.findall('staging'):
            rebuild = stg.find('rebuild').text
            suppkg_list = stg.find('supportpkg').text
            need_rebuild = False
            suppkgs = []
            if suppkg_list:
                suppkgs = suppkg_list.split(',')

            stgname = stg.get('name')
            if len(cand_sources[stgname]) and rebuild == 'unknown':
                need_rebuild = True
                stg.find('rebuild').text = 'needed'
                new_suppkg_list = ','.join(cand_sources[stgname])
                stg.find('supportpkg').text = new_suppkg_list
            elif len(cand_sources[stgname]):
                for cand in cand_sources[stgname]:
                    if cand not in suppkgs:
                        need_rebuild = True
                        stg.find('rebuild').text = 'needed'
                        break
                new_suppkg_list = ','.join(cand_sources[stgname])
                stg.find('supportpkg').text = new_suppkg_list
            else:
                stg.find('rebuild').text = 'unneeded'
                stg.find('supportpkg').text = ''

            if stg.find('rebuild').text == 'needed':
                need_rebuild = True

            if need_rebuild and not self.api.is_repo_dirty(stgname, 'standard'):
                logging.info(f'Rebuild {stgname}')
                osc.core.rebuild(self.apiurl, stgname, None, None, None)
                stg.find('rebuild').text = 'unneeded'

        rebuild_data_updated = ET.tostring(root).decode()
        logging.debug(rebuild_data_updated)
        if rebuild_data_updated != rebuild_data:
            logging.info('Updating support pkg list...')
            self.api.pseudometa_file_save(
                'support_pkg_rebuild', rebuild_data_updated, 'support package rebuild')


def main(args):
    # Configure OSC
    osc.conf.get_config(override_apiurl=args.apiurl)
    osc.conf.config['debug'] = args.debug

    uc = StagingHelper(args.project)
    uc.crawl()


if __name__ == '__main__':
    description = 'Rebuild project if support package were staged in the staging project'
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument('-A', '--apiurl', metavar='URL', help='API URL')
    parser.add_argument('-d', '--debug', action='store_true',
                        help='print info useful for debuging')
    parser.add_argument(
        '-p',
        '--project',
        dest='project',
        metavar='PROJECT',
        help=f'deafult project (default: {OPENSUSE})',
        default=OPENSUSE,
    )

    args = parser.parse_args()

    # Set logging configuration
    logging.basicConfig(level=logging.DEBUG if args.debug
                        else logging.INFO)

    sys.exit(main(args))
