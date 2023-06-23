#!/usr/bin/python3

from lxml import etree as ET
import sys
import cmdln
import logging
from urllib.error import HTTPError

import ToolBase

logger = logging.getLogger()

FACTORY = "openSUSE:Factory"


class BiArchTool(ToolBase.ToolBase):

    def __init__(self, project):
        ToolBase.ToolBase.__init__(self)
        self.project = project
        self.biarch_packages = None
        self._has_baselibs = {}
        self.packages = []
        self.arch = 'i586'
        self.rdeps = None
        self.package_metas = {}
        self.whitelist = {
            'i586': {
                'bzr',
                'git',
                'libjpeg62-turbo',
                'mercurial',
                'subversion',
                'ovmf',
            }
        }
        self.blacklist = {
            'i586': {
                'belle-sip',
                'release-notes-openSUSE',
                'openSUSE-EULAs',
                'skelcd-openSUSE',
                'plasma5-workspace',
                'patterns-base',
                'patterns-fonts',
                'patterns-rpm-macros',
                'patterns-yast',
                '000release-packages',
            }
        }

    def get_filelist(self, project, package, expand=False):
        query = {}
        if expand:
            query['expand'] = 1
        root = ET.fromstring(self.cached_GET(self.makeurl(['source', self.project, package], query)))
        return [node.get('name') for node in root.findall('entry')]

    def has_baselibs(self, package):
        if package in self._has_baselibs:
            return self._has_baselibs[package]

        is_multibuild = False
        srcpkgname = package
        if ':' in package:
            is_multibuild = True
            srcpkgname = package.split(':')[0]

        ret = False
        files = self.get_filelist(self.project, srcpkgname)
        if 'baselibs.conf' in files:
            logger.debug('%s has baselibs', package)
            if is_multibuild:
                logger.warning('%s is multibuild and has baselibs. canot handle that!', package)
            else:
                ret = True
        elif '_link' in files:
            files = self.get_filelist(self.project, srcpkgname, expand=True)
            if 'baselibs.conf' in files:
                logger.warning('%s is linked to a baselibs package', package)
        elif is_multibuild:
            logger.warning('%s is multibuild', package)
        self._has_baselibs[package] = ret
        return ret

    def is_biarch_recursive(self, package):
        logger.debug(package)
        if package in self.blacklist[self.arch]:
            logger.debug('%s is blacklisted', package)
            return False
        if package in self.biarch_packages:
            logger.debug('%s is known biarch package', package)
            return True
        if package in self.whitelist[self.arch]:
            logger.debug('%s is whitelisted', package)
            return True
        r = self.has_baselibs(package)
        if r:
            return r
        if package in self.rdeps:
            for p in self.rdeps[package]:
                r = self.is_biarch_recursive(p)
                if r:
                    break
        return r

    def _init_biarch_packages(self):
        if self.biarch_packages is None:
            if ':Rings' in self.project:
                self.biarch_packages = set()
            else:
                self.biarch_packages = set(
                    self.meta_get_packagelist(f"{self.project}:Rings:0-Bootstrap")
                )
                self.biarch_packages |= set(
                    self.meta_get_packagelist(f"{self.project}:Rings:1-MinimalX")
                )

        self._init_rdeps()
        self.fill_package_meta()

    def fill_package_meta(self):
        url = self.makeurl(['search', 'package'], f"match=[@project='{self.project}']")
        root = ET.fromstring(self.cached_GET(url))
        for p in root.findall('package'):
            name = p.attrib['name']
            self.package_metas[name] = p

    def _init_rdeps(self):
        if self.rdeps is not None:
            return
        self.rdeps = {}
        url = self.makeurl(['build', self.project, 'standard', self.arch, '_builddepinfo'], {'view': 'revpkgnames'})
        x = ET.fromstring(self.cached_GET(url))
        for pnode in x.findall('package'):
            name = pnode.get('name')
            for depnode in pnode.findall('pkgdep'):
                depname = depnode.text
                if depname == name:
                    logger.warning('%s requires itself for build', name)
                    continue
                self.rdeps.setdefault(name, set()).add(depname)

    def select_packages(self, packages):
        if packages == '__all__':
            self.packages = self.meta_get_packagelist(self.project)
        elif packages == '__latest__':
            self.packages = self.latest_packages(self.project)
        else:
            self.packages = packages

    def remove_explicit_enable(self):

        self._init_biarch_packages()

        resulturl = self.makeurl(['build', self.project, '_result'])
        result = ET.fromstring(self.cached_GET(resulturl))

        packages = {
            n.get('package')
            for n in result.findall(f"./result[@arch='{self.arch}']/status")
            if n.get('code') not in ('disabled', 'excluded')
        }
        for pkg in sorted(packages):
            changed = False

            logger.debug("processing %s", pkg)
            if pkg not in self.package_metas:
                logger.error("%s not found", pkg)
                continue
            pkgmeta = self.package_metas[pkg]

            for build in pkgmeta.findall("./build"):
                for n in build.findall(f"./enable[@arch='{self.arch}']"):
                    logger.debug("disable %s", pkg)
                    build.remove(n)
                    changed = True

            if changed:
                try:
                    pkgmetaurl = self.makeurl(['source', self.project, pkg, '_meta'])
                    self.http_PUT(pkgmetaurl, data=ET.tostring(pkgmeta))
                    if self.caching:
                        self._invalidate__cached_GET(pkgmetaurl)
                except HTTPError as e:
                    logger.error('failed to update %s: %s', pkg, e)

    def add_explicit_disable(self, wipebinaries=False):

        self._init_biarch_packages()

        for pkg in self.packages:

            changed = False

            logger.debug("processing %s", pkg)
            if pkg not in self.package_metas:
                logger.error("%s not found", pkg)
                continue
            pkgmeta = self.package_metas[pkg]

            build = pkgmeta.findall("./build")
            if not build:
                logger.debug('disable %s for %s', pkg, self.arch)
                bn = pkgmeta.find('build')
                if bn is None:
                    bn = ET.SubElement(pkgmeta, 'build')
                ET.SubElement(bn, 'disable', {'arch': self.arch})
                changed = True

            if changed:
                try:
                    pkgmetaurl = self.makeurl(['source', self.project, pkg, '_meta'])
                    self.http_PUT(pkgmetaurl, data=ET.tostring(pkgmeta))
                    if self.caching:
                        self._invalidate__cached_GET(pkgmetaurl)
                    if wipebinaries:
                        self.http_POST(self.makeurl(['build', self.project], {
                            'cmd': 'wipe',
                            'arch': self.arch,
                            'package': pkg}))
                except HTTPError as e:
                    logger.error('failed to update %s: %s', pkg, e)

    def enable_baselibs_packages(self, force=False, wipebinaries=False):
        self._init_biarch_packages()
        todo = {}
        for pkg in self.packages:
            logger.debug("processing %s", pkg)
            if pkg not in self.package_metas:
                logger.error("%s not found", pkg)
                continue
            pkgmeta = self.package_metas[pkg]

            is_enabled = None
            is_disabled = None
            must_disable = None
            changed = None

            for _ in pkgmeta.findall(f"./build/enable[@arch='{self.arch}']"):
                is_enabled = True
            for _ in pkgmeta.findall(f"./build/disable[@arch='{self.arch}']"):
                is_disabled = True

            if force:
                must_disable = False

            if must_disable is None:
                must_disable = not self.is_biarch_recursive(pkg)
            if must_disable:
                if not is_disabled:
                    logger.info('disabling %s for %s', pkg, self.arch)
                    bn = pkgmeta.find('build')
                    if bn is None:
                        bn = ET.SubElement(pkgmeta, 'build')
                    ET.SubElement(bn, 'disable', {'arch': self.arch})
                    changed = True
                else:
                    logger.debug('%s already disabled for %s', pkg, self.arch)

            elif is_disabled:
                logger.info('enabling %s for %s', pkg, self.arch)
                for build in pkgmeta.findall("./build"):
                    for n in build.findall(f"./disable[@arch='{self.arch}']"):
                        build.remove(n)
                        changed = True
                if not changed:
                    logger.error('build tag not found in %s/%s!?', pkg, self.arch)
            else:
                logger.debug('%s already enabled for %s', pkg, self.arch)
            if is_enabled:
                logger.info('removing explicit enable %s for %s', pkg, self.arch)
                for build in pkgmeta.findall("./build"):
                    for n in build.findall(f"./enable[@arch='{self.arch}']"):
                        build.remove(n)
                        changed = True
                if not changed:
                    logger.error('build tag not found in %s/%s!?', pkg, self.arch)

            if changed:
                todo[pkg] = pkgmeta

        if todo:
            logger.info("applying changes")
        for pkg in sorted(todo.keys()):
            pkgmeta = todo[pkg]
            try:
                pkgmetaurl = self.makeurl(['source', self.project, pkg, '_meta'])
                self.http_PUT(pkgmetaurl, data=ET.tostring(pkgmeta))
                if self.caching:
                    self._invalidate__cached_GET(pkgmetaurl)

                if (
                    wipebinaries
                    and pkgmeta.find(f"./build/disable[@arch='{self.arch}']")
                    is not None
                ):
                    logger.debug("wiping %s", pkg)
                    self.http_POST(self.makeurl(['build', self.project], {
                        'cmd': 'wipe',
                        'arch': self.arch,
                        'package': pkg}))
            except HTTPError as e:
                logger.error('failed to update %s: %s', pkg, e)


class CommandLineInterface(ToolBase.CommandLineInterface):

    def __init__(self, *args, **kwargs):
        ToolBase.CommandLineInterface.__init__(self, args, kwargs)

    def get_optparser(self):
        parser = ToolBase.CommandLineInterface.get_optparser(self)
        parser.add_option(
            '-p',
            '--project',
            dest='project',
            metavar='PROJECT',
            help=f'project to process (default: {FACTORY})',
            default=FACTORY,
        )
        return parser

    def setup_tool(self):
        return BiArchTool(self.options.project)

    def _select_packages(self, all, packages):
        if packages:
            self.tool.select_packages(packages)
        elif all:
            self.tool.select_packages('__all__')
        else:
            self.tool.select_packages('__latest__')

    @cmdln.option('-n', '--interval', metavar="minutes", type="int", help="periodic interval in minutes")
    @cmdln.option('-a', '--all', action='store_true', help='process all packages')
    @cmdln.option('-f', '--force', action='store_true', help='enable in any case')
    @cmdln.option('--wipe', action='store_true', help='also wipe binaries')
    def do_enable_baselibs_packages(self, subcmd, opts, *packages):
        """${cmd_name}: enable build for packages in Ring 0 or 1 or with
        baselibs.conf

        ${cmd_usage}
        ${cmd_option_list}
        """
        def work():
            self._select_packages(opts.all, packages)
            self.tool.enable_baselibs_packages(force=opts.force, wipebinaries=opts.wipe)

        self.runner(work, opts.interval)

    @cmdln.option('-a', '--all', action='store_true', help='process all packages')
    def do_remove_explicit_enable(self, subcmd, opts, *packages):
        """${cmd_name}: remove all explicit enable tags from packages

        ${cmd_usage}
        ${cmd_option_list}
        """

        self.tool.remove_explicit_enable()

    @cmdln.option('-a', '--all', action='store_true', help='process all packages')
    @cmdln.option('-n', '--interval', metavar="minutes", type="int", help="periodic interval in minutes")
    @cmdln.option('--wipe', action='store_true', help='also wipe binaries')
    def do_add_explicit_disable(self, subcmd, opts, *packages):
        """${cmd_name}: add explicit disable to all packages

        ${cmd_usage}
        ${cmd_option_list}
        """

        def work():
            self._select_packages(opts.all, packages)
            self.tool.add_explicit_disable(wipebinaries=opts.wipe)

        self.runner(work, opts.interval)


if __name__ == "__main__":
    app = CommandLineInterface()
    sys.exit(app.main())
