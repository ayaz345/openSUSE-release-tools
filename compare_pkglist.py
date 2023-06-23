#!/usr/bin/python3

import argparse
import logging
import sys
from urllib.error import HTTPError

from lxml import etree as ET
import osc.conf
import osc.core

OPENSUSE = 'openSUSE:Leap:15.2'
SLE = 'SUSE:SLE-15-SP2:GA'

makeurl = osc.core.makeurl
http_GET = osc.core.http_GET
http_POST = osc.core.http_POST


class CompareList(object):
    def __init__(self, old_prj, new_prj, verbose, newonly, removedonly, existin, submit, submitfrom, submitto, submit_limit):
        self.new_prj = new_prj
        self.old_prj = old_prj
        self.verbose = verbose
        self.newonly = newonly
        self.existin = existin
        self.submit = submit
        self.submitfrom = submitfrom
        self.submitto = submitto
        self.submit_limit = submit_limit
        self.removedonly = removedonly
        self.apiurl = osc.conf.config['apiurl']
        self.debug = osc.conf.config['debug']

    def get_source_packages(self, project):
        """Return the list of packages in a project."""
        query = {'expand': 1}
        root = ET.parse(http_GET(makeurl(self.apiurl, ['source', project],
                                 query=query))).getroot()
        return [i.get('name') for i in root.findall('entry')]

    def item_exists(self, project, package=None):
        """
        Return true if the given project or package exists
        """
        if package:
            url = makeurl(self.apiurl, ['source', project, package, '_meta'])
        else:
            url = makeurl(self.apiurl, ['source', project, '_meta'])
        try:
            http_GET(url)
        except HTTPError:
            return False
        return True

    def removed_pkglist(self, project):
        apiurl = 'https://api.suse.de' if project.startswith('SUSE:') else self.apiurl
        query = f"match=state/@name='accepted'+and+(action/target/@project='{project}'+and+action/@type='delete')"
        url = makeurl(apiurl, ['search', 'request'], query)
        f = http_GET(url)
        root = ET.parse(f).getroot()
        return [t.get('package') for t in root.findall('./request/action/target')]

    def is_linked_package(self, project, package):
        query = {'withlinked': 1}
        u = makeurl(self.apiurl, ['source', project, package], query=query)
        root = ET.parse(http_GET(u)).getroot()
        links = root.findall('linkinfo/linked')
        if links is None:
            return False

        return not any(
            linked.get('project') == project
            and linked.get('package').startswith(f"{package}.")
            for linked in links
        )

    def check_diff(self, package, old_prj, new_prj):
        logging.debug(f'checking {package} ...')
        query = {'cmd': 'diff',
                 'view': 'xml',
                 'oproject': old_prj,
                 'opackage': package}
        u = makeurl(self.apiurl, ['source', new_prj, package], query=query)
        root = ET.parse(http_POST(u)).getroot()
        old_srcmd5 = root.findall('old')[0].get('srcmd5')
        logging.debug(f'{package} old srcmd5 {old_srcmd5} in {old_prj}')
        new_srcmd5 = root.findall('new')[0].get('srcmd5')
        logging.debug(f'{package} new srcmd5 {new_srcmd5} in {new_prj}')
        # Compare srcmd5
        if old_srcmd5 != new_srcmd5:
            if diffs := root.findall('files/file/diff'):
                return ET.tostring(root)
        return False

    def submit_new_package(self, source, target, package, msg=None):
        if req := osc.core.get_request_list(
            self.apiurl, target, package, req_state=('new', 'review', 'declined')
        ):
            print(f"There is a request to {target} / {package} already, skip!")
        else:
            if not msg:
                msg = 'New package submitted by compare_pkglist'
            res = osc.core.create_submit_request(self.apiurl, source, package, target, package, message=msg)
            if res and res is not None:
                print(f'Created request {res} for {package}')
                return True
            else:
                print('Error occurred when creating the submit request')
        return False

    def crawl(self):
        """Main method"""
        if self.submit:
            if (self.submitfrom and not self.submitto) or (self.submitto and not self.submitfrom):
                print("** Please give both --submitfrom and --submitto parameter **")
                return
            if self.submitfrom:
                if not self.item_exists(self.submitfrom):
                    print(f"Project {self.submitfrom} is not exist")
                    return
                if not self.item_exists(self.submitto):
                    print(f"Project {self.submitto} is not exist")
                    return

        # get souce packages from target
        print(f'Gathering the package list from {self.old_prj}')
        source = self.get_source_packages(self.old_prj)
        print(f'Gathering the package list from {self.new_prj}')
        target = self.get_source_packages(self.new_prj)
        removed_packages = self.removed_pkglist(self.old_prj)
        if self.existin:
            print(f'Gathering the package list from {self.existin}')
            existin_packages = self.get_source_packages(self.existin)

        if not self.removedonly:
            dest = self.submitto if self.submitto else self.new_prj
            removed_pkgs_in_target = self.removed_pkglist(dest)
            submit_counter = 0
            for pkg in source:
                if pkg.startswith('00') or pkg.startswith('_'):
                    continue

                if pkg not in target:
                    # ignore the second specfile package
                    linked = self.is_linked_package(self.old_prj, pkg)
                    if linked:
                        continue

                    if self.existin:
                        if pkg not in existin_packages:
                            continue

                    if pkg in removed_pkgs_in_target:
                        print("New package but has removed from {:<8} - {}".format(self.new_prj, pkg))
                        continue

                    print("New package than {:<8} - {}".format(self.new_prj, pkg))

                    if self.submit:
                        if self.submit_limit and submit_counter > int(self.submit_limit):
                            return

                        if self.submitfrom and self.submitto:
                            if not self.item_exists(self.submitfrom, pkg):
                                print(f"{pkg} not found in {self.submitfrom}")
                                continue
                            msg = f"Automated submission of a package from {self.submitfrom} to {self.submitto}"
                            if self.existin:
                                msg += f" that was included in {self.existin}"
                            if self.submit_new_package(self.submitfrom, self.submitto, pkg, msg):
                                submit_counter += 1
                        else:
                            msg = f"Automated submission of a package from {self.old_prj} that is new in {self.new_prj}"
                            if self.submit_new_package(self.old_prj, self.new_prj, pkg, msg):
                                submit_counter += 1
                elif not self.newonly:
                    if diff := self.check_diff(pkg, self.old_prj, self.new_prj):
                        print("Different source in {:<8} - {}".format(self.new_prj, pkg))
                        if self.verbose:
                            print(f"=== Diff ===\n{diff}")

        for pkg in removed_packages:
            if pkg in target:
                print("Deleted package in {:<8} - {}".format(self.old_prj, pkg))


def main(args):
    # Configure OSC
    osc.conf.get_config(override_apiurl=args.apiurl)
    osc.conf.config['debug'] = args.debug

    uc = CompareList(args.old_prj, args.new_prj, args.verbose, args.newonly,
                     args.removedonly, args.existin, args.submit, args.submitfrom, args.submitto, args.submit_limit)
    uc.crawl()


if __name__ == '__main__':
    description = 'Compare packages status between two project'
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument('-A', '--apiurl', metavar='URL', help='API URL')
    parser.add_argument('-d', '--debug', action='store_true',
                        help='print info useful for debuging')
    parser.add_argument(
        '-o',
        '--old',
        dest='old_prj',
        metavar='PROJECT',
        help=f'the old project where to compare (default: {SLE})',
        default=SLE,
    )
    parser.add_argument(
        '-n',
        '--new',
        dest='new_prj',
        metavar='PROJECT',
        help=f'the new project where to compare (default: {OPENSUSE})',
        default=OPENSUSE,
    )
    parser.add_argument('-v', '--verbose', action='store_true',
                        help='show the diff')
    parser.add_argument('--newonly', action='store_true',
                        help='show new package only')
    parser.add_argument('--removedonly', action='store_true',
                        help='show removed package but exists in target')
    parser.add_argument('--existin', dest='existin', metavar='PROJECT',
                        help='the package exists in the project')
    parser.add_argument('--submit', action='store_true', default=False,
                        help='submit new package to target, FROM and TO can re-configureable by --submitfrom and --submitto')
    parser.add_argument('--submitfrom', dest='submitfrom', metavar='PROJECT',
                        help='submit new package from, define --submitto is required')
    parser.add_argument('--submitto', dest='submitto', metavar='PROJECT',
                        help='submit new package to, define --submitfrom is required')
    parser.add_argument('--limit', dest='submit_limit', metavar='NUMBERS',
                        help='limit numbers packages to submit')

    args = parser.parse_args()

    # Set logging configuration
    logging.basicConfig(level=logging.DEBUG if args.debug
                        else logging.INFO)

    sys.exit(main(args))
