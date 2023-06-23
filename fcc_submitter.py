#!/usr/bin/python3

import argparse
import logging
import sys
import time

from urllib.error import HTTPError, URLError

import random
import re
from lxml import etree as ET

import osc.conf
import osc.core
from osclib.core import devel_project_get
from osclib.core import project_pseudometa_package

OPENSUSE = 'openSUSE:Leap:15.2'
OPENSUSE_PREVERSION = 'openSUSE:Leap:15.1'
OPENSUSE_RELEASED_VERSION = ['openSUSE:Leap:15.0', 'openSUSE:Leap:15.1']
FCC = f'{OPENSUSE}:FactoryCandidates'

makeurl = osc.core.makeurl
http_GET = osc.core.http_GET
http_POST = osc.core.http_POST
http_PUT = osc.core.http_PUT


class FccFreezer(object):
    def __init__(self):
        self.factory = 'openSUSE:Factory'
        self.apiurl = osc.conf.config['apiurl']
        self.debug = osc.conf.config['debug']

    def list_packages(self, project):
        url = makeurl(self.apiurl, ['source', project])
        root = ET.parse(http_GET(url)).getroot()
        xmllines = root.findall("./entry")
        pkglist = [pkg.attrib['name'] for pkg in xmllines]
        return set(pkglist)

    def check_one_source(self, flink, si, pkglist, pkglist_prever):
        """
        Insert package information to the temporary frozenlinks.
        Return package name if the package can not fit the condition
        add to the frozenlinks, can be the ignored package.
        """
        package = si.get('package')
        logging.debug(f"Processing {package}")

        # If the package is an internal one (e.g _product)
        if package.startswith('_') or package.startswith('Test-DVD') or package.startswith('000'):
            return None

        # filter out multibuild package
        for originpackage in si.findall('originpackage'):
            if ':' in package and package.split(':')[0] == originpackage.text:
                return package

        for linked in si.findall('linked'):
            if linked.get('project') == self.factory:
                if linked.get('package') in pkglist or linked.get('package') in pkglist_prever:
                    return package
                url = makeurl(self.apiurl, ['source', self.factory, package], {'view': 'info', 'nofilename': '1'})
                # print(package, linked.get('package'), linked.get('project'))
                f = http_GET(url)
                proot = ET.parse(f).getroot()
                lsrcmd5 = proot.get('lsrcmd5')
                if lsrcmd5 is None:
                    raise Exception(f"{self.factory}/{package} is not a link but we expected one")
                ET.SubElement(flink, 'package', {'name': package, 'srcmd5': lsrcmd5, 'vrev': si.get('vrev')})
                return None

        if package in pkglist or package in pkglist_prever:
            return package

        if package in ['rpmlint-mini-AGGR']:
            # we should not freeze aggregates
            return None

        ET.SubElement(flink, 'package', {'name': package, 'srcmd5': si.get('srcmd5'), 'vrev': si.get('vrev')})
        return None

    def receive_sources(self, flink):
        ignored_sources = []
        pkglist = self.list_packages(OPENSUSE)
        # we also don't want the package is exist in the previous version
        pkglist_prever = self.list_packages(OPENSUSE_PREVERSION)

        url = makeurl(self.apiurl, ['source', self.factory], {'view': 'info', 'nofilename': '1'})
        f = http_GET(url)
        root = ET.parse(f).getroot()

        for si in root.findall('sourceinfo'):
            package = self.check_one_source(flink, si, pkglist, pkglist_prever)
            if package is not None:
                ignored_sources.append(str(package))
        return ignored_sources

    def freeze(self):
        """Main method"""
        flink = ET.Element('frozenlinks')

        fl = ET.SubElement(flink, 'frozenlink', {'project': self.factory})
        ignored_sources = self.receive_sources(fl)
        if self.debug:
            logging.debug("Dump ignored source")
            for source in ignored_sources:
                logging.debug(f"Ignored source: {source}")

        url = makeurl(self.apiurl, ['source', FCC, '_project', '_frozenlinks'], {'meta': '1'})
        link = ET.tostring(flink)
        try:
            http_PUT(url, data=link)
        except HTTPError as e:
            raise e


class FccSubmitter(object):
    def __init__(self, from_prj, to_prj, submit_limit):
        self.from_prj = from_prj
        self.to_prj = to_prj
        self.factory = 'openSUSE:Factory'
        self.submit_limit = int(submit_limit)
        self.apiurl = osc.conf.config['apiurl']
        self.debug = osc.conf.config['debug']
        self.sle_base_prjs = [
            'SUSE:SLE-15-SP2:GA',
            'SUSE:SLE-15-SP1:Update',
            'SUSE:SLE-15-SP1:GA',
            'SUSE:SLE-15:Update',
            'SUSE:SLE-15:GA',
            'SUSE:SLE-12-SP4:Update',
            'SUSE:SLE-12-SP4:GA',
            'SUSE:SLE-12-SP3:Update',
            'SUSE:SLE-12-SP3:GA',
            'SUSE:SLE-12-SP2:Update',
            'SUSE:SLE-12-SP2:GA',
            'SUSE:SLE-12-SP1:Update',
            'SUSE:SLE-12-SP1:GA',
            'SUSE:SLE-12:Update',
            'SUSE:SLE-12:GA']
        # the skip list against devel project
        self.skip_devel_project_list = [
            'mobile:synchronization:FACTORY']
        # put the except packages from skip_devel_project_list, use regex in this list, eg. "^golang-x-(\w+)", "^nodejs$"
        self.except_pkgs_list = []

    def get_source_packages(self, project, expand=False):
        """Return the list of packages in a project."""
        query = {'expand': 1} if expand else {}
        root = ET.parse(http_GET(makeurl(self.apiurl, ['source', project],
                                 query=query))).getroot()
        return [i.get('name') for i in root.findall('entry')]

    def get_request_list(self, package):
        return osc.core.get_request_list(self.apiurl, self.to_prj, package, req_state=('new', 'review', 'declined', 'revoked'))

    def get_link(self, project, package):
        try:
            link = http_GET(makeurl(self.apiurl, ['source', project, package, '_link'])).read()
        except (HTTPError, URLError):
            return None
        return ET.fromstring(link)

    def get_build_succeeded_packages(self, project):
        """Get the build succeeded packages from `from_prj` project.
        """

        f = osc.core.show_prj_results_meta(self.apiurl, project)
        root = ET.fromstringlist(f)
        # print ET.dump(root)

        failed_multibuild_pacs = []
        pacs = []
        for node in root.findall('result'):
            if node.get('repository') == 'standard' and node.get('arch') == 'x86_64':
                for pacnode in node.findall('status'):
                    if ':' in pacnode.get('package'):
                        mainpac = pacnode.get('package').split(':')[0]
                        if pacnode.get('code') not in ['succeeded', 'excluded']:
                            failed_multibuild_pacs.append(pacnode.get('package'))
                            if mainpac not in failed_multibuild_pacs:
                                failed_multibuild_pacs.append(mainpac)
                            if mainpac in pacs:
                                pacs.remove(mainpac)
                        elif mainpac in failed_multibuild_pacs:
                            failed_multibuild_pacs.append(pacnode.get('package'))
                        elif mainpac not in pacs:
                            pacs.append(mainpac)
                        continue
                    if pacnode.get('code') == 'succeeded':
                        pacs.append(pacnode.get('package'))
            else:
                logging.error("Can not find standard/x86_64 results")

        return pacs

    def is_new_package(self, tgt_project, tgt_package):
        try:
            logging.debug(f"Gathering package_meta {tgt_project}/{tgt_package}")
            osc.core.show_package_meta(self.apiurl, tgt_project, tgt_package)
        except (HTTPError, URLError):
            return True
        return False

    def create_submitrequest(self, package):
        """Create a submit request using the osc.commandline.Osc class."""
        src_project = self.factory  # submit from Factory only
        dst_project = self.to_prj

        msg = f'Automatic request from {src_project} by F-C-C Submitter. Please review this change and decline it if Leap do not need it.'
        return osc.core.create_submit_request(
            self.apiurl, src_project, package, dst_project, package, message=msg
        )

    def check_multiple_specfiles(self, project, package):
        try:
            url = makeurl(self.apiurl, ['source', project, package], {'expand': '1'})
        except HTTPError as e:
            if e.code == 404:
                return None
            raise e
        root = ET.fromstring(http_GET(url).read())
        linkinfo = root.find('linkinfo')
        data = {
            'linkinfo': linkinfo.attrib['package']
            if linkinfo is not None
            else None
        }
        files = [entry.get('name').replace('.spec', '')
                 for entry in root.findall('entry') if entry.get('name').endswith('.spec')]
        data['specs'] = files

        return data if len(files) > 1 else False

    def is_sle_base_pkgs(self, package):
        link = self.get_link(self.to_prj, package)
        if link is None or link.get('project') not in self.sle_base_prjs:
            logging.debug(f"{package} not from SLE base")
            return False
        return True

    def list_pkgs(self):
        """List build succeeded packages"""
        succeeded_packages = []
        succeeded_packages = self.get_build_succeeded_packages(self.from_prj)
        if len(succeeded_packages) <= 0:
            logging.info(f'No build succeeded package in {self.from_prj}')
            return

        print('Build succeeded packages:')
        print('-------------------------------------')
        for pkg in succeeded_packages:
            print(pkg)

        print('-------------------------------------')
        print(f"Found {len(succeeded_packages)} build succeded packages")

    def get_deleted_packages(self, project):
        query = 'states=accepted&types=delete&project={}&view=collection'
        query = query.format(project)
        url = makeurl(self.apiurl, ['request'], query)
        f = http_GET(url)
        root = ET.parse(f).getroot()

        pkgs = []
        for sr in root.findall('request'):
            tgt_package = sr.find('action').find('target').get('package')
            pkgs.append(tgt_package)

        return pkgs

    def load_skip_pkgs_list(self, project, package):
        url = makeurl(
            self.apiurl, ['source', project, package, 'fcc_skip_pkgs?expand=1']
        )
        try:
            return http_GET(url).read()
        except HTTPError:
            return ''

    def crawl(self):
        """Main method"""
        succeeded_packages = []
        succeeded_packages = self.get_build_succeeded_packages(self.from_prj)
        if len(succeeded_packages) <= 0:
            logging.info(f'No build succeeded package in {self.from_prj}')
            return

        # randomize the list
        random.shuffle(succeeded_packages)
        # get souce packages from target
        target_packages = self.get_source_packages(self.to_prj)
        deleted_packages = self.get_deleted_packages(self.to_prj)
        if self.to_prj.startswith("openSUSE:"):
            for prd in OPENSUSE_RELEASED_VERSION:
                deleted_packages = deleted_packages + self.get_deleted_packages(prd)

        pseudometa_project, pseudometa_package = project_pseudometa_package(self.apiurl, 'openSUSE:Factory')
        skip_pkgs_list = self.load_skip_pkgs_list(pseudometa_project, pseudometa_package).splitlines()

        ms_packages = []  # collect multi specs packages

        for i in range(0, min(int(self.submit_limit), len(succeeded_packages))):
            package = succeeded_packages[i]
            submit_ok = True

            if package in deleted_packages:
                logging.info(f'{package} has been dropped from {self.to_prj}, ignore it!')
                submit_ok = False

            if self.is_sle_base_pkgs(package) is True:
                logging.info(f'{package} origin from SLE base, skip for now!')
                submit_ok = False

            # make sure it is new package
            new_pkg = self.is_new_package(self.to_prj, package)
            if new_pkg is not True:
                logging.info(f'{package} is not a new package, do not submit.')
                submit_ok = False

            multi_specs = self.check_multiple_specfiles(self.factory, package)
            if multi_specs is None:
                logging.info(f'{package} does not exist in openSUSE:Factory')
                submit_ok = False

            if multi_specs:
                if multi_specs['linkinfo']:
                    logging.info(
                        f"{package} in openSUSE:Factory is sub-package of {multi_specs['linkinfo']}, skip it!"
                    )
                    ms_packages.append(package)
                    submit_ok = False

                for spec in multi_specs['specs']:
                    if spec not in succeeded_packages:
                        logging.info(f'{spec} is sub-pacakge of {package} but build failed, skip it!')
                        submit_ok = False

            if not submit_ok:
                continue

            # make sure the package non-exist in target yet ie. expand=False
            if package in target_packages:
                logging.debug(f'{package} is exist in {self.to_prj}, skip!')
            elif request := self.get_request_list(package):
                logging.debug(
                    f"There is a request to {package} / {self.to_prj} already or it has been declined/revoked, skip!"
                )
            else:
                logging.info("%d - Preparing submit %s to %s" % (i, package, self.to_prj))
                # get devel project
                devel_prj, devel_pkg = devel_project_get(self.apiurl, self.factory, package)
                    # check devel project does not in the skip list
                if devel_prj in self.skip_devel_project_list:
                    # check the except packages list
                    match = None
                    for elem in self.except_pkgs_list:
                        m = re.search(elem, package)
                        if m is not None:
                            match = True

                    if match is not True:
                        logging.info(f'{devel_prj}/{package} is in the skip list, do not submit.')
                        continue
                # check package does not in the skip list
                match = None
                for elem in skip_pkgs_list:
                    m = re.search(str(elem), package)
                    if m is not None:
                        match = True

                if match is True:
                    logging.info(f'{package} is in the skip list, do not submit.')
                    continue
                res = self.create_submitrequest(package)
                if res and res is not None:
                    logging.info(f'Created request {res} for {package}')
                else:
                    logging.error('Error occurred when creating submit request')
            time.sleep(5)

        # dump multi specs packages
        print("Multi-specfile packages:")
        if ms_packages:
            for pkg in ms_packages:
                print(pkg)
        else:
            print('None')


def main(args):
    # Configure OSC
    osc.conf.get_config(override_apiurl=args.apiurl)
    osc.conf.config['debug'] = args.debug

    if args.freeze:
        print(f'freezing {FCC}')
        freezer = FccFreezer()
        freezer.freeze()
    else:
        uc = FccSubmitter(args.from_prj, args.to_prj, args.submit_limit)
        if args.list_packages:
            uc.list_pkgs()
        else:
            uc.crawl()


if __name__ == '__main__':
    description = 'Create SR from FactoryCandidates to '\
                  'openSUSE Leap project for new build succeded packages.'
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument('-A', '--apiurl', metavar='URL', help='API URL')
    parser.add_argument('-d', '--debug', action='store_true',
                        help='print info useful for debuging')
    parser.add_argument(
        '-f',
        '--from',
        dest='from_prj',
        metavar='PROJECT',
        help=f'project where to check (default: {FCC})',
        default=FCC,
    )
    parser.add_argument(
        '-t',
        '--to',
        dest='to_prj',
        metavar='PROJECT',
        help=f'project where to submit the packages (default: {OPENSUSE})',
        default=OPENSUSE,
    )
    parser.add_argument('-r', '--freeze', dest='freeze', action='store_true', help='rebase FCC project')
    parser.add_argument('-s', '--list', dest='list_packages', action='store_true', help='list build succeeded packages')
    parser.add_argument('-l', '--limit', dest='submit_limit', metavar='NUMBERS',
                        help='limit numbers packages to submit (default: 100)', default=100)

    args = parser.parse_args()

    # Set logging configuration
    logging.basicConfig(level=logging.DEBUG if args.debug
                        else logging.INFO)

    sys.exit(main(args))
