#!/usr/bin/python3

from pprint import pformat
from stat import S_ISREG, S_ISLNK
from tempfile import NamedTemporaryFile
import cmdln
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import abichecker_dbmodel as DB
import sqlalchemy.orm.exc

from lxml import etree as ET

import osc.conf
import osc.core
from osc.util.cpio import CpioRead

from urllib.error import HTTPError

import rpm
from collections import namedtuple
from osclib.comments import CommentAPI

from abichecker_common import CACHEDIR

import ReviewBot

WEB_URL=None

# build mapping between source repos and target repos
MR = namedtuple('MatchRepo', ('srcrepo', 'dstrepo', 'arch'))

# FIXME: use attribute instead
PROJECT_BLACKLIST = {
    'SUSE:SLE-11:Update'     : "abi-checker doesn't support SLE 11",
    'SUSE:SLE-11-SP1:Update' : "abi-checker doesn't support SLE 11",
    'SUSE:SLE-11-SP2:Update' : "abi-checker doesn't support SLE 11",
    'SUSE:SLE-11-SP3:Update' : "abi-checker doesn't support SLE 11",
    'SUSE:SLE-11-SP4:Update' : "abi-checker doesn't support SLE 11",
    'SUSE:SLE-11-SP5:Update' : "abi-checker doesn't support SLE 11",
    }

# some project have more repos than what we are interested in
REPO_WHITELIST = {
        'openSUSE:Factory':      ('standard', 'snapshot'),
        'SUSE:SLE-12:Update' :   'standard',
        }

# same for arch
ARCH_BLACKLIST = {
        }

ARCH_WHITELIST = {
        'SUSE:SLE-12:Update' :   ('i586', 'ppc64le', 's390', 's390x', 'x86_64'),
        }

# Directory where download binary packages.
DOWNLOADS = os.path.join(CACHEDIR, 'downloads')
UNPACKDIR = os.path.join(CACHEDIR, 'unpacked')

so_re = re.compile(r'^(?:/usr)?/lib(?:64)?/lib([^/]+)\.so(?:\.[^/]+)?')
debugpkg_re = re.compile(r'-debug(?:source|info)(?:-(?:32|64)bit)?$')
disturl_re = re.compile(r'^obs://[^/]+/(?P<prj>[^/]+)/(?P<repo>[^/]+)/(?P<md5>[0-9a-f]{32})-(?P<pkg>.*)$')

comment_marker_re = re.compile(r'<!-- abichecker state=(?P<state>done|seen)(?: result=(?P<result>accepted|declined))? -->')

# report for source submissions. contains multiple libresult for each library
Report = namedtuple('Report', ('src_project', 'src_package', 'src_rev', 'dst_project', 'dst_package', 'reports', 'result'))
# report for a single library
LibResult = namedtuple('LibResult', ('src_repo', 'src_lib', 'dst_repo', 'dst_lib', 'arch', 'htmlreport', 'result'))


class DistUrlMismatch(Exception):
    def __init__(self, disturl, md5):
        Exception.__init__(self)
        self.msg = f'disturl mismatch has: {disturl} wanted ...{md5}'

    def __str__(self):
        return self.msg


class SourceBroken(Exception):
    def __init__(self, project, package):
        Exception.__init__(self)
        self.msg = f'{project}/{package} has broken sources, needs rebase'

    def __str__(self):
        return self.msg


class NoBuildSuccess(Exception):
    def __init__(self, project, package, md5):
        Exception.__init__(self)
        self.msg = f'{project}/{package}({md5}) had no successful build'

    def __str__(self):
        return self.msg


class NotReadyYet(Exception):
    def __init__(self, project, package, reason):
        Exception.__init__(self)
        self.msg = f'{project}/{package} not ready yet: {reason}'

    def __str__(self):
        return self.msg


class MissingDebugInfo(Exception):
    def __init__(self, missing_debuginfo):
        Exception.__init__(self)
        self.msg = ''
        for i in missing_debuginfo:
            if len(i) == 6:
                self.msg += "%s/%s %s/%s %s %s\n"%i
            elif len(i) == 5:
                self.msg += "%s/%s %s/%s %s\n"%i

    def __str__(self):
        return self.msg


class FetchError(Exception):
    def __init__(self, msg):
        Exception.__init__(self)
        self.msg = msg

    def __str__(self):
        return self.msg


class MaintenanceError(Exception):
    def __init__(self, msg):
        Exception.__init__(self)
        self.msg = msg

    def __str__(self):
        return self.msg


class LogToDB(logging.Filter):
    def __init__(self, session):
        self.session = session
        self.request_id = None

    def filter(self, record):
        if self.request_id is not None and record.levelno >= logging.INFO:
            logentry = DB.Log(request_id = self.request_id, line = record.getMessage())
            self.session.add(logentry)
            self.session.commit()
        return True


class ABIChecker(ReviewBot.ReviewBot):
    """ check ABI of library packages
    """

    def __init__(self, *args, **kwargs):
        ReviewBot.ReviewBot.__init__(self, *args, **kwargs)

        self.no_review = False
        self.force = False

        self.ts = rpm.TransactionSet()
        self.ts.setVSFlags(rpm._RPMVSF_NOSIGNATURES)

        # reports of source submission
        self.reports = []
        # textual report summary for use in accept/decline message
        # or comments
        self.text_summary = ''

        self.session = DB.db_session()

        self.dblogger = LogToDB(self.session)

        self.logger.addFilter(self.dblogger)

        self.commentapi = CommentAPI(self.apiurl)

        self.current_request = None

    def check_source_submission(self, src_project, src_package, src_rev, dst_project, dst_package):

        # happens for maintenance incidents
        if dst_project is None and src_package == 'patchinfo':
            return None

        if dst_project in PROJECT_BLACKLIST:
            self.logger.info(PROJECT_BLACKLIST[dst_project])
#            self.text_summary += PROJECT_BLACKLIST[dst_project] + "\n"
            return True

        # default is to accept the review, just leave a note if
        # there were problems.
        ret = True

        if self.has_staging(dst_project):
            # if staged we don't look at the request source but what
            # is in staging
            if self.current_request.staging_project:
                src_project = self.current_request.staging_project
                src_package = dst_package
                src_rev = None
            else:
                self.logger.debug("request not staged yet")
                return None

        ReviewBot.ReviewBot.check_source_submission(self, src_project, src_package, src_rev, dst_project, dst_package)

        report = Report(src_project, src_package, src_rev, dst_project, dst_package, [], None)

        dst_srcinfo = self.get_sourceinfo(dst_project, dst_package)
        self.logger.debug('dest sourceinfo %s', pformat(dst_srcinfo))
        if dst_srcinfo is None:
            msg = f"{dst_project}/{dst_package} seems to be a new package, no need to review"
            self.logger.info(msg)
            self.reports.append(report)
            return True
        src_srcinfo = self.get_sourceinfo(src_project, src_package, src_rev)
        self.logger.debug('src sourceinfo %s', pformat(src_srcinfo))
        if src_srcinfo is None:
            msg = f"{src_project}/{src_package}@{src_rev} does not exist!? can't check"
            self.logger.error(msg)
            self.text_summary += msg + "\n"
            self.reports.append(report)
            return False

        if os.path.exists(UNPACKDIR):
            shutil.rmtree(UNPACKDIR)

        try:
            # compute list of common repos to find out what to compare
            myrepos = self.findrepos(src_project, src_srcinfo, dst_project, dst_srcinfo)
        except NoBuildSuccess as e:
            self.logger.info(e)
            self.text_summary += "**Error**: %s\n"%e
            self.reports.append(report)
            return False
        except NotReadyYet as e:
            self.logger.info(e)
            self.reports.append(report)
            return None
        except SourceBroken as e:
            self.logger.error(e)
            self.text_summary += "**Error**: %s\n"%e
            self.reports.append(report)
            return False

        if not myrepos:
            self.text_summary += "**Error**: %s does not build against %s, can't check library ABIs\n\n"%(src_project, dst_project)
            self.logger.info("no matching repos, can't compare")
            self.reports.append(report)
            return False

        # *** beware of nasty maintenance stuff ***
        # if the destination is a maintained project we need to
        # mangle our comparison target and the repo mapping
        try:
            originproject, originpackage, origin_srcinfo, new_repo_map = self._maintenance_hack(dst_project, dst_srcinfo, myrepos)
            if originproject is not None:
                dst_project = originproject
            if originpackage is not None:
                dst_package = originpackage
            if origin_srcinfo is not None:
                dst_srcinfo = origin_srcinfo
            if new_repo_map is not None:
                myrepos = new_repo_map
        except MaintenanceError as e:
            self.text_summary += "**Error**: %s\n\n"%e
            self.logger.error('%s', e)
            self.reports.append(report)
            return False
        except NoBuildSuccess as e:
            self.logger.info(e)
            self.text_summary += "**Error**: %s\n"%e
            self.reports.append(report)
            return False
        except NotReadyYet as e:
            self.logger.info(e)
            self.reports.append(report)
            return None
        except SourceBroken as e:
            self.logger.error(e)
            self.text_summary += "**Error**: %s\n"%e
            self.reports.append(report)
            return False

        notes = []
        libresults = []

        overall = None

        missing_debuginfo  = []

        for mr in myrepos:
            try:
                dst_libs, dst_libdebug = self.extract(dst_project, dst_package, dst_srcinfo, mr.dstrepo, mr.arch)
                # nothing to fetch, so no libs
                if dst_libs is None:
                    continue
            except DistUrlMismatch as e:
                self.logger.error(f"{dst_project}/{dst_package} {mr.dstrepo}/{mr.arch}: {e}")
                if ret == True: # need to check again
                    ret = None
                continue
            except MissingDebugInfo as e:
                missing_debuginfo.append(str(e))
                ret = False
                continue
            except FetchError as e:
                self.logger.error(e)
                if ret: # need to check again
                    ret = None
                continue

            try:
                src_libs, src_libdebug = self.extract(src_project, src_package, src_srcinfo, mr.srcrepo, mr.arch)
                if src_libs is None:
                    if dst_libs:
                        self.text_summary += "*Warning*: the submission does not contain any libs anymore\n\n"
                    continue
            except DistUrlMismatch as e:
                self.logger.error(f"{src_project}/{src_package} {mr.srcrepo}/{mr.arch}: {e}")
                if ret == True: # need to check again
                    ret = None
                continue
            except MissingDebugInfo as e:
                missing_debuginfo.append(str(e))
                ret = False
                continue
            except FetchError as e:
                self.logger.error(e)
                if ret: # need to check again
                    ret = None
                continue

            # create reverse index for aliases in the source project
            src_aliases = {}
            for lib in src_libs.keys():
                for a in src_libs[lib]:
                    src_aliases.setdefault(a, set()).add(lib)

            # for each library in the destination project check if the same lib
            # exists in the source project. If not check the aliases (symlinks)
            # to catch soname changes. Generate pairs of matching libraries.
            pairs = set()
            for lib in dst_libs.keys():
                if lib in src_libs:
                    pairs.add((lib, lib))
                else:
                    self.logger.debug("%s not found in submission, checking aliases", lib)
                    found = False
                    for a in dst_libs[lib]:
                        if a in src_aliases:
                            for l in src_aliases[a]:
                                pairs.add((lib, l))
                                found = True
                    if found == False:
                        self.text_summary += "*Warning*: %s no longer packaged\n\n"%lib

            self.logger.debug("to diff: %s", pformat(pairs))

            # for each pair dump and compare the abi
            for old, new in pairs:
                # abi dump of old lib
                old_base = os.path.join(UNPACKDIR, dst_project, dst_package, mr.dstrepo, mr.arch)
                old_dump = os.path.join(CACHEDIR, 'old.dump')
                # abi dump of new lib
                new_base = os.path.join(UNPACKDIR, src_project, src_package, mr.srcrepo, mr.arch)
                new_dump = os.path.join(CACHEDIR, 'new.dump')

                def cleanup():
                    if os.path.exists(old_dump):
                        os.unlink(old_dump)
                    if os.path.exists(new_dump):
                        os.unlink(new_dump)

                cleanup()

                # we just need that to pass a name to abi checker
                m = so_re.match(old)
                htmlreport = 'report-%s-%s-%s-%s-%s-%08x.html'%(mr.srcrepo, os.path.basename(old), mr.dstrepo, os.path.basename(new), mr.arch, int(time.time()))

                # run abichecker
                if m \
                        and self.run_abi_dumper(old_dump, old_base, old, dst_libdebug[old]) \
                        and self.run_abi_dumper(new_dump, new_base, new, src_libdebug[new]):
                    reportfn = os.path.join(CACHEDIR, htmlreport)
                    r = self.run_abi_checker(m.group(1), old_dump, new_dump, reportfn)
                    if r is not None:
                        self.logger.debug('report saved to %s, compatible: %d', reportfn, r)
                        libresults.append(LibResult(mr.srcrepo, os.path.basename(old), mr.dstrepo, os.path.basename(new), mr.arch, htmlreport, r))
                        if overall is None:
                            overall = r
                        elif overall == True and r == False:
                            overall = r
                else:
                    self.logger.error(f'failed to compare {old} <> {new}')
                    self.text_summary += "**Error**: ABI check failed on %s vs %s\n\n"%(old, new)
                    if ret == True: # need to check again
                        ret = None

                cleanup()

        if missing_debuginfo:
            self.text_summary += 'debug information is missing for the following packages, can\'t check:\n<pre>'
            self.text_summary += ''.join(missing_debuginfo)
            self.text_summary += '</pre>\nplease enable debug info in your project config.\n'

        self.reports.append(report._replace(result = overall, reports = libresults))

        # upload reports

        if os.path.exists(UNPACKDIR):
            shutil.rmtree(UNPACKDIR)

        return ret

    def _maintenance_hack(self, dst_project, dst_srcinfo, myrepos):
        pkg = dst_srcinfo.package
        originproject = None
        originpackage = None

        # find the maintenance project
        url = osc.core.makeurl(
            self.apiurl,
            ('search', 'project', 'id'),
            f"match=(maintenance/maintains/@project='{dst_project}'+and+attribute/@name='{osc.conf.config['maintenance_attribute']}')",
        )
        root = ET.parse(osc.core.http_GET(url)).getroot()
        if root is not None:
            node = root.find('project')
            if node is not None:
                # check if target project is a project link where the
                # sources don't actually build (like openSUSE:...:Update). That
                # is the case if no update was released yet.
                # XXX: TODO: do check for whether the package builds here first
                originproject = self.get_originproject(dst_project, pkg)
                if originproject is not None:
                    self.logger.debug("origin project %s", originproject)
                    url = osc.core.makeurl(self.apiurl, ('build', dst_project, '_result'), { 'package': pkg })
                    root = ET.parse(osc.core.http_GET(url)).getroot()
                    alldisabled = True
                    for node in root.findall('status'):
                        if node.get('code') != 'disabled':
                            alldisabled = False
                    if alldisabled:
                        self.logger.debug(f"all repos disabled, using originproject {originproject}")
                    else:
                        originproject = None
                else:
                    mproject = node.attrib['name']
                    # packages are only a link to packagename.incidentnr
                    (linkprj, linkpkg) = self._get_linktarget(dst_project, pkg)
                    if linkpkg is not None and linkprj == dst_project:
                        self.logger.debug(f"{dst_project}/{pkg} links to {linkpkg}")
                        regex = re.compile(r'.*\.(\d+)$')
                        m = regex.match(linkpkg)
                        if m is None:
                            raise MaintenanceError(
                                f"{dst_project}/{pkg} -> {linkprj}/{linkpkg} is not a proper maintenance link (must match /{regex.pattern}/)"
                            )
                        incident = m[1]
                        self.logger.debug(f"is maintenance incident {incident}")

                        originproject = f"{mproject}:{incident}"
                        originpackage = f'{pkg}.' + dst_project.replace(':', '_')

                        origin_srcinfo = self.get_sourceinfo(originproject, originpackage)
                        if origin_srcinfo is None:
                            raise MaintenanceError(f"{originproject}/{originpackage} invalid")

                        # find the map of maintenance incident repos to destination repos
                        originrepos = self.findrepos(originproject, origin_srcinfo, dst_project, dst_srcinfo)
                        mapped = {(mr.dstrepo, mr.arch): mr for mr in originrepos}
                        self.logger.debug("mapping: %s", pformat(mapped))

                        # map the repos of the original request to the maintenance incident repos
                        matchrepos = set()
                        for mr in myrepos:
                            if (mr.dstrepo, mr.arch) not in mapped:
                                # sometimes a previously released maintenance
                                # update didn't cover all architectures. We can
                                # only ignore that then.
                                self.logger.warning(
                                    f"couldn't find repo {mr.dstrepo}/{mr.arch} in {originproject}/{originpackage}"
                                )
                                continue
                            matchrepos.add(MR(mr.srcrepo, mapped[(mr.dstrepo, mr.arch)].srcrepo, mr.arch))

                        myrepos = matchrepos
                        dst_srcinfo = origin_srcinfo
                        self.logger.debug("new repo map: %s", pformat(myrepos))

        return (originproject, originpackage, dst_srcinfo, myrepos)


    def find_abichecker_comment(self, req):
        """Return previous comments (should be one)."""
        comments = self.commentapi.get_comments(request_id=req.reqid)
        for c in comments.values():
            if m := comment_marker_re.match(c['comment']):
                return c['id'], m.group('state'), m.group('result')
        return None, None, None

    def check_one_request(self, req):

        self.review_messages = ReviewBot.ReviewBot.DEFAULT_REVIEW_MESSAGES

        if self.no_review and not self.force and self.check_request_already_done(req.reqid):
            self.logger.info("skip request %s which is already done", req.reqid)
            # TODO: check if the request was seen before and we
            # didn't reach a final state for too long
            return None

        commentid, state, result = self.find_abichecker_comment(req)
## using comments instead of db would be an options for bots
## that use no db
#        if self.no_review:
#            if state == 'done':
#                self.logger.debug("request %s already done, result: %s"%(req.reqid, result))
#                return

        self.dblogger.request_id = req.reqid

        self.current_request = req

        self.reports = []
        self.text_summary = ''
        try:
            ret = ReviewBot.ReviewBot.check_one_request(self, req)
        except Exception as e:
            import traceback
            self.logger.error("unhandled exception in ABI checker")
            self.logger.error(traceback.format_exc())
            ret = None

        result = None
        if ret is not None:
            state = 'done'
            result = 'accepted' if ret else 'declined'
        else:
            # we probably don't want abichecker to spam here
            # FIXME don't delete comment in this case
            #if state is None and not self.text_summary:
            #    self.text_summary = 'abichecker will take a look later'
            state = 'seen'

        self.save_reports_to_db(req, state, result)
        if ret is not None and not self.text_summary:
            # if for some reason save_reports_to_db didn't produce a
            # summary we add one
            self.text_summary = (
                f"ABI checker result: [{result}]({WEB_URL}/request/{req.reqid})"
            )

        if commentid and not self.dryrun:
            self.commentapi.delete(commentid)

        self.post_comment(req, state, result)

        self.review_messages = { 'accepted': self.text_summary, 'declined': self.text_summary }

        if self.no_review:
            ret = None

        self.dblogger.request_id = None

        self.current_request = None

        return ret

    def check_request_already_done(self, reqid):
        try:
            request = self.session.query(DB.Request).filter(DB.Request.id == reqid).one()
            if request.state == 'done':
                return True
        except sqlalchemy.orm.exc.NoResultFound as e:
            pass

        return False

    def save_reports_to_db(self, req, state, result):
        try:
            request = self.session.query(DB.Request).filter(DB.Request.id == req.reqid).one()
            for i in self.session.query(DB.ABICheck).filter(DB.ABICheck.request_id == request.id).all():
                # yeah, we could be smarter here and update existing reports instead
                self.session.delete(i)
            self.session.flush()
            request.state = state
            request.result = result
        except sqlalchemy.orm.exc.NoResultFound as e:
            request = DB.Request(id = req.reqid,
                    state = state,
                    result = result,
                    )
            self.session.add(request)
        self.session.commit()
        for r in self.reports:
            abicheck = DB.ABICheck(
                    request = request,
                    src_project = r.src_project,
                    src_package = r.src_package,
                    src_rev = r.src_rev,
                    dst_project = r.dst_project,
                    dst_package = r.dst_package,
                    result = r.result
                    )
            self.session.add(abicheck)
            self.session.commit()
            if r.result is None:
                continue
            elif r.result:
                self.text_summary += "Good news from ABI check, "
                self.text_summary += "%s seems to be ABI [compatible](%s/request/%s):\n\n"%(r.dst_package, WEB_URL, req.reqid)
            else:
                self.text_summary += "Warning: bad news from ABI check, "
                self.text_summary += "%s may be ABI [**INCOMPATIBLE**](%s/request/%s):\n\n"%(r.dst_package, WEB_URL, req.reqid)
            for lr in r.reports:
                libreport = DB.LibReport(
                        abicheck = abicheck,
                        src_repo = lr.src_repo,
                        src_lib = lr.src_lib,
                        dst_repo = lr.dst_repo,
                        dst_lib = lr.dst_lib,
                        arch = lr.arch,
                        htmlreport = lr.htmlreport,
                        result = lr.result,
                        )
                self.session.add(libreport)
                self.session.commit()
                self.text_summary += "* %s (%s): [%s](%s/report/%d)\n"%(lr.dst_lib, lr.arch,
                    "compatible" if lr.result else "***INCOMPATIBLE***",
                    WEB_URL, libreport.id)

        self.reports = []

    def post_comment(self, req, state, result):
        if not self.text_summary:
            return

        msg = "<!-- abichecker state=%s%s -->\n" % (
            state,
            f' result={result}' if result else '',
        )
        msg += self.text_summary

        self.logger.info(f"add comment: {msg}")
        if not self.dryrun:
            #self.commentapi.delete_from_where_user(self.review_user, request_id = req.reqid)
            self.commentapi.add_comment(request_id = req.reqid, comment = msg)

    def run_abi_checker(self, libname, old, new, output):
        cmd = ['abi-compliance-checker',
                '-lib', libname,
                '-old', old,
                '-new', new,
                '-report-path', output
                ]
        self.logger.debug(cmd)
        r = subprocess.Popen(cmd, close_fds=True, cwd=CACHEDIR).wait()
        if r not in (0, 1):
            self.logger.error('abi-compliance-checker failed')
            # XXX: record error
            return None
        return r == 0

    def run_abi_dumper(self, output, base, filename, debuglib):
        cmd = ['abi-dumper',
                '-o', output,
                '-lver', os.path.basename(filename),
                '/'.join([base, filename])]
        cmd.append('/'.join([base, debuglib]))
        self.logger.debug(cmd)
        r = subprocess.Popen(cmd, close_fds=True, cwd=CACHEDIR).wait()
        if r != 0:
            self.logger.error(f"failed to dump {filename}!")
            # XXX: record error
            return False
        return True

    def extract(self, project, package, srcinfo, repo, arch):
        # fetch cpio headers
        # check file lists for library packages
        fetchlist, liblist, debuglist = self.compute_fetchlist(project, package, srcinfo, repo, arch)

        if not fetchlist:
            msg = f"no libraries found in {project}/{package} {repo}/{arch}"
            self.logger.info(msg)
            return None, None

        # mtimes in cpio are not the original ones, so we need to fetch
        # that separately :-(
        mtimes= self._getmtimes(project, package, repo, arch)

        self.logger.debug("fetchlist %s", pformat(fetchlist))
        self.logger.debug("liblist %s", pformat(liblist))
        self.logger.debug("debuglist %s", pformat(debuglist))

        debugfiles = debuglist.values()

        # fetch binary rpms
        downloaded = self.download_files(project, package, repo, arch, fetchlist, mtimes)

        # extract binary rpms
        tmpfile = os.path.join(CACHEDIR, "cpio")
        for fn in fetchlist:
            self.logger.debug(f"extract {fn}")
            with open(tmpfile, 'wb') as tmpfd:
                if fn not in downloaded:
                    raise FetchError(f"{fn} was not downloaded!")
                self.logger.debug(downloaded[fn])
                r = subprocess.call(['rpm2cpio', downloaded[fn]], stdout=tmpfd, close_fds=True)
                if r != 0:
                    raise FetchError(f"failed to extract {fn}!")
                tmpfd.close()
                os.unlink(downloaded[fn])
                cpio = CpioRead(tmpfile)
                cpio.read()
                for ch in cpio:
                    fn = ch.filename.decode('utf-8')
                    if fn.startswith('./'): # rpm payload is relative
                        fn = fn[1:]
                    self.logger.debug("cpio fn %s", fn)
                    if fn not in liblist and fn not in debugfiles:
                        continue
                    dst = os.path.join(UNPACKDIR, project, package, repo, arch)
                    dst += fn
                    if not os.path.exists(os.path.dirname(dst)):
                        os.makedirs(os.path.dirname(dst))
                    self.logger.debug("dst %s", dst)
                    # the filehandle in the cpio archive is private so
                    # open it again
                    with open(tmpfile, 'rb') as cpiofh:
                        cpiofh.seek(ch.dataoff, os.SEEK_SET)
                        with open(dst, 'wb') as fh:
                            while True:
                                buf = cpiofh.read(4096)
                                if buf is None or buf == b'':
                                    break
                                fh.write(buf)
        os.unlink(tmpfile)

        return liblist, debuglist

    def download_files(self, project, package, repo, arch, filenames, mtimes):
        downloaded = {}
        for fn in filenames:
            if fn not in mtimes:
                raise FetchError(f"missing mtime information for {fn}, can't check")
            repodir = os.path.join(DOWNLOADS, package, project, repo)
            if not os.path.exists(repodir):
                os.makedirs(repodir)
            t = os.path.join(repodir, fn)
            self._get_binary_file(project, repo, arch, package, fn, t, mtimes[fn])
            downloaded[fn] = t
        return downloaded

    def _get_binary_file(self, project, repository, arch, package, filename, target, mtime):
        """Get a binary file from OBS."""
        # Used to cache, but dropped as part of python3 migration.
        osc.core.get_binary_file(self.apiurl, project, repository, arch,
                                 filename, package=package,
                                 target_filename=target)

    def readRpmHeaderFD(self, fd):
        h = None
        try:
            h = self.ts.hdrFromFdno(fd)
        except rpm.error as e:
            if str(e) == "public key not available":
                print(e)
            if str(e) == "public key not trusted":
                print(e)
            if str(e) == "error reading package header":
                print(e)
            h = None
        return h

    def _fetchcpioheaders(self, project, package, repo, arch):
        u = osc.core.makeurl(self.apiurl, [ 'build', project, repo, arch, package ],
            [ 'view=cpioheaders' ])
        try:
            r = osc.core.http_GET(u)
        except HTTPError as e:
            raise FetchError(f'failed to fetch header information: {e}')
        tmpfile = NamedTemporaryFile(prefix="cpio-", delete=False)
        for chunk in r:
            tmpfile.write(chunk)
        tmpfile.close()
        cpio = CpioRead(tmpfile.name)
        cpio.read()
        rpm_re = re.compile('(.+\.rpm)-[0-9A-Fa-f]{32}$')
        for ch in cpio:
            # ignore errors
            if ch.filename == '.errors':
                continue
            # the filehandle in the cpio archive is private so
            # open it again
            with open(tmpfile.name, 'rb') as fh:
                fh.seek(ch.dataoff, os.SEEK_SET)
                h = self.readRpmHeaderFD(fh)
                if h is None:
                    raise FetchError(f"failed to read rpm header for {ch.filename}")
                if m := rpm_re.match(ch.filename.decode('utf-8')):
                    yield (m[1], h)
        os.unlink(tmpfile.name)

    def _getmtimes(self, prj, pkg, repo, arch):
        """ returns a dict of filename: mtime """
        url = osc.core.makeurl(self.apiurl, ('build', prj, repo, arch, pkg))
        try:
            root = ET.parse(osc.core.http_GET(url)).getroot()
        except HTTPError:
            return None

        return dict([(node.attrib['filename'], node.attrib['mtime']) for node in root.findall('binary')])

    # modified from repochecker
    def _last_build_success(self, src_project, tgt_project, src_package, rev):
        """Return the last build success XML document from OBS."""
        try:
            query = { 'lastsuccess' : 1,
                    'package' : src_package,
                    'pathproject' : tgt_project,
                    'srcmd5' : rev }
            url = osc.core.makeurl(self.apiurl, ('build', src_project, '_result'), query)
            return ET.parse(osc.core.http_GET(url)).getroot()
        except HTTPError as e:
            if e.code != 404:
                self.logger.error(f'ERROR in URL {url} [{e}]')
                raise
        return None

    def get_buildsuccess_repos(self, src_project, tgt_project, src_package, rev):
        root = self._last_build_success(src_project, tgt_project, src_package, rev)
        if root is None:
            return None

        # build list of repos as set of (name, arch) tuples
        repos = set()
        for repo in root.findall('repository'):
            name = repo.attrib['name']
            for node in repo.findall('arch'):
                repos.add((name, node.attrib['arch']))

        self.logger.debug("success repos: %s", pformat(repos))

        return repos

    def get_dstrepos(self, project):
        url = osc.core.makeurl(self.apiurl, ('source', project, '_meta'))
        try:
            root = ET.parse(osc.core.http_GET(url)).getroot()
        except HTTPError:
            return None

        repos = set()
        for repo in root.findall('repository'):
            name = repo.attrib['name']
            if project in REPO_WHITELIST and name not in REPO_WHITELIST[project]:
                continue

            for node in repo.findall('arch'):
                arch = node.text

                if project in ARCH_WHITELIST and arch not in ARCH_WHITELIST[project]:
                    continue

                if project in ARCH_BLACKLIST and arch in ARCH_BLACKLIST[project]:
                    continue

                repos.add((name, arch))

        return repos

    def ensure_settled(self, src_project, src_srcinfo, matchrepos):
        """ make sure current build state is final so we're not
        tricked with half finished results"""
        rmap = {}
        results = osc.core.get_package_results(self.apiurl,
                src_project, src_srcinfo.package,
                repository = [ mr.srcrepo for mr in matchrepos],
                arch = [ mr.arch for mr in matchrepos])
        for result in results:
            for res, _ in osc.core.result_xml_to_dicts(result):
                if 'package' not in res or res['package'] != src_srcinfo.package:
                    continue
                rmap[(res['repository'], res['arch'])] = res

        for mr in matchrepos:
            if (mr.srcrepo, mr.arch) not in rmap:
                self.logger.warning(f"{mr.srcrepo}/{mr.arch} had no build success")
                raise NotReadyYet(src_project, src_srcinfo.package, "no result")
            if rmap[(mr.srcrepo, mr.arch)]['dirty']:
                self.logger.warning(f"{mr.srcrepo}/{mr.arch} dirty")
                raise NotReadyYet(src_project, src_srcinfo.package, "dirty")
            code = rmap[(mr.srcrepo, mr.arch)]['code']
            if code == 'broken':
                raise SourceBroken(src_project, src_srcinfo.package)
            if code not in ['succeeded', 'locked', 'excluded']:
                self.logger.warning(f"{mr.srcrepo}/{mr.arch} not succeeded ({code})")
                raise NotReadyYet(src_project, src_srcinfo.package, code)

    def findrepos(self, src_project, src_srcinfo, dst_project, dst_srcinfo):

        # get target repos that had a successful build
        dstrepos = self.get_dstrepos(dst_project)
        if dstrepos is None:
            return None

        url = osc.core.makeurl(self.apiurl, ('source', src_project, '_meta'))
        try:
            root = ET.parse(osc.core.http_GET(url)).getroot()
        except HTTPError:
            return None

        # set of source repo name, target repo name, arch
        matchrepos = set()
        # XXX: another staging hack
        if self.current_request.staging_project:
            for node in root.findall("repository[@name='standard']/arch"):
                arch = node.text
                self.logger.debug('arch %s', arch)
                matchrepos.add(MR('standard', 'standard', arch))
        else:
            for repo in root.findall('repository'):
                name = repo.attrib['name']
                path = repo.findall('path')
                if path is None or len(path) != 1:
                    self.logger.error(f"repo {name} has more than one path")
                    continue
                prj = path[0].attrib['project']
                if prj == 'openSUSE:Tumbleweed':
                    prj = 'openSUSE:Factory' # XXX: hack
                if prj != dst_project:
                    continue
                for node in repo.findall('arch'):
                    arch = node.text
                    dstname = path[0].attrib['repository']
                    if prj == 'openSUSE:Factory' and dstname == 'snapshot':
                        dstname = 'standard' # XXX: hack
                    if (dstname, arch) in dstrepos:
                        matchrepos.add(MR(name, dstname, arch))

        if not matchrepos:
            return None
        else:
            self.logger.debug('matched repos %s', pformat(matchrepos))

        # make sure it's not dirty
        self.ensure_settled(src_project, src_srcinfo, matchrepos)

        # now check if all matched repos built successfully
        srcrepos = self.get_buildsuccess_repos(src_project, dst_project, src_srcinfo.package, src_srcinfo.verifymd5)
        if srcrepos is None:
            raise NotReadyYet(src_project, src_srcinfo.package, "no build success")
        if not srcrepos:
            raise NoBuildSuccess(src_project, src_srcinfo.package, src_srcinfo.verifymd5)
        for mr in matchrepos:
            if (mr.srcrepo, arch) not in srcrepos:
                self.logger.error(f"{mr.srcrepo}/{arch} had no build success")
                raise NoBuildSuccess(src_project, src_srcinfo.package, src_srcinfo.verifymd5)

        return matchrepos

    # common with repochecker
    def _md5_disturl(self, disturl):
        """Get the md5 from the DISTURL from a RPM file."""
        return os.path.basename(disturl).split('-')[0]

    def disturl_matches_md5(self, disturl, md5):
        return self._md5_disturl(disturl) == md5

    # this is a bit magic. OBS allows to take the disturl md5 from the package
    # and query the source info for that. We will then get the verify md5 that
    # belongs to that md5.
    def disturl_matches(self, disturl, prj, srcinfo):
        md5 = self._md5_disturl(disturl)
        info = self.get_sourceinfo(prj, srcinfo.package, rev = md5)
        self.logger.debug(pformat(srcinfo))
        self.logger.debug(pformat(info))
        return info.verifymd5 == srcinfo.verifymd5

    def compute_fetchlist(self, prj, pkg, srcinfo, repo, arch):
        """ scan binary rpms of the specified repo for libraries.
        Returns a set of packages to fetch and the libraries found
        """
        self.logger.debug('scanning %s/%s %s/%s'%(prj, pkg, repo, arch))

        headers = self._fetchcpioheaders(prj, pkg, repo, arch)
        missing_debuginfo = set()
        lib_packages = dict() # pkgname -> set(lib file names)
        pkgs = dict() # pkgname -> cpiohdr, rpmhdr
        lib_aliases = dict()
        for rpmfn, h in headers:
            # skip src rpm
            if h['sourcepackage']:
                continue
            pkgname = h['name'].decode('utf-8')
            if pkgname.endswith('-32bit') or pkgname.endswith('-64bit'):
                # -32bit and -64bit packages are just repackaged, so
                # we skip them and only check the original one.
                continue
            self.logger.debug("inspecting %s", pkgname)
            if not self.disturl_matches(h['disturl'].decode('utf-8'), prj, srcinfo):
                raise DistUrlMismatch(h['disturl'].decode('utf-8'), srcinfo)
            pkgs[pkgname] = (rpmfn, h)
            if debugpkg_re.match(pkgname):
                continue
            for fn, mode, lnk in zip(h['filenames'], h['filemodes'], h['filelinktos']):
                fn = fn.decode('utf-8')
                lnk = lnk.decode('utf-8')
                if so_re.match(fn):
                    if S_ISREG(mode):
                        self.logger.debug('found lib: %s'%fn)
                        lib_packages.setdefault(pkgname, set()).add(fn)
                    elif S_ISLNK(mode) and lnk is not None:
                        alias = os.path.basename(fn)
                        libname = os.path.basename(lnk)
                        self.logger.debug('found alias: %s -> %s'%(alias, libname))
                        lib_aliases.setdefault(libname, set()).add(alias)

        fetchlist = set()
        liblist = dict()
        debuglist = dict()
        # check whether debug info exists for each lib
        for pkgname in sorted(lib_packages.keys()):
            dpkgname = pkgname+'-debuginfo'
            if dpkgname not in pkgs:
                missing_debuginfo.add((prj, pkg, repo, arch, pkgname))
                continue

            # check file list of debuginfo package
            rpmfn, h = pkgs[dpkgname]
            files = set ([f.decode('utf-8') for f in h['filenames']])
            ok = True
            for lib in lib_packages[pkgname]:
                libdebug = '/usr/lib/debug%s.debug'%lib
                if libdebug not in files:
                    # some new format that includes version, release and arch in debuginfo?
                    # FIXME: version and release are actually the
                    # one from the main package, sub packages may
                    # differ. BROKEN RIGHT NOW
                    # XXX: would have to actually read debuglink
                    # info to get that right so just guessing
                    arch = h['arch'].decode('utf-8')
                    if arch == 'i586':
                        arch = 'i386'
                    libdebug = '/usr/lib/debug%s-%s-%s.%s.debug'%(lib,
                            h['version'].decode('utf-8'), h['release'].decode('utf-8'), arch)
                    if libdebug not in files:
                        missing_debuginfo.add((prj, pkg, repo, arch, pkgname, lib))
                        ok = False

                if ok:
                    fetchlist.add(pkgs[pkgname][0])
                    fetchlist.add(rpmfn)
                    liblist.setdefault(lib, set())
                    debuglist.setdefault(lib, libdebug)
                    libname = os.path.basename(lib)
                    if libname in lib_aliases:
                        liblist[lib] |= lib_aliases[libname]

        if missing_debuginfo:
            self.logger.error('missing debuginfo: %s'%pformat(missing_debuginfo))
            raise MissingDebugInfo(missing_debuginfo)

        return fetchlist, liblist, debuglist

class CommandLineInterface(ReviewBot.CommandLineInterface):

    def __init__(self, *args, **kwargs):
        ReviewBot.CommandLineInterface.__init__(self, args, kwargs)
        self.clazz = ABIChecker

    def get_optparser(self):
        parser = ReviewBot.CommandLineInterface.get_optparser(self)
        parser.add_option("--force", action="store_true", help="recheck requests that are already considered done")
        parser.add_option("--no-review", action="store_true", help="don't actually accept or decline, just comment")
        parser.add_option("--web-url", metavar="URL", help="URL of web service")
        return parser

    def postoptparse(self):
        ret = ReviewBot.CommandLineInterface.postoptparse(self)
        if self.options.web_url is not None:
            global WEB_URL
            WEB_URL = self.options.web_url
        else:
            self.optparser.error("must specify --web-url")
            ret = False
        return ret

    def setup_checker(self):
        bot = ReviewBot.CommandLineInterface.setup_checker(self)

        if self.options.no_review:
            bot.no_review = True
        if self.options.force:
            bot.force = True

        return bot

    @cmdln.option('-r', '--revision', metavar="number", type="int", help="revision number")
    def do_diff(self, subcmd, opts, src_project, src_package, dst_project, dst_package):
        src_rev = opts.revision
        print(self.checker.check_source_submission(src_project, src_package, src_rev, dst_project, dst_package))

if __name__ == "__main__":
    app = CommandLineInterface()
    sys.exit( app.main() )
