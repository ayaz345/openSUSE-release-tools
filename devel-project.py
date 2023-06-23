#!/usr/bin/python3

import argparse
from datetime import datetime
import sys
from lxml import etree as ET

import osc.conf
from osc.core import HTTPError
from osc.core import get_review_list
from osc.core import show_package_meta
from osc.core import show_project_meta
from osclib.comments import CommentAPI
from osclib.conf import Config
from osclib.core import devel_project_fallback
from osclib.core import entity_email
from osclib.core import get_request_list_with_history
from osclib.core import package_list_kind_filtered
from osclib.core import request_age
from osclib.stagingapi import StagingAPI
from osclib.util import mail_send


BOT_NAME = 'devel-project'
REMINDER = 'review reminder'


def search(apiurl, queries=None, **kwargs):
    if 'request' in kwargs:
        # get_review_list() does not support withfullhistory, but search() does.
        if queries is None:
            queries = {}
        request = queries.get('request', {})
        request['withfullhistory'] = 1
        queries['request'] = request

    return osc.core._search(apiurl, queries, **kwargs)


osc.core._search = osc.core.search
osc.core.search = search


def staging_api(args):
    apiurl = osc.conf.config['apiurl']
    Config(apiurl, args.project)
    return StagingAPI(apiurl, args.project)


def devel_projects_get(apiurl, project):
    """
    Returns a sorted list of devel projects for a given project.

    Loads all packages for a given project, checks them for a devel link and
    keeps a list of unique devel projects.
    """
    root = search(apiurl, **{'package': f"@project='{project}'"})['package']
    devel_projects = {
        devel.attrib['project']: True
        for devel in root.findall('package/devel[@project]')
    }
    # Ensure self does not end up in list.
    if project in devel_projects:
        del devel_projects[project]

    return sorted(devel_projects)


def list(args):
    devel_projects = devel_projects_get(osc.conf.config['apiurl'], args.project)
    if len(devel_projects) == 0:
        print('no devel projects found')
    else:
        out = '\n'.join(devel_projects)
        print(out)

        if args.write:
            api = staging_api(args)
            api.pseudometa_file_ensure('devel_projects', out, 'devel_projects write')


def devel_projects_load(args):
    api = staging_api(args)
    if devel_projects := api.pseudometa_file_load('devel_projects'):
        return devel_projects.splitlines()

    raise Exception('no devel projects found')


def maintainer(args):
    if args.group is None:
        # Default is appended to rather than overridden (upstream bug).
        args.group = ['factory-maintainers', 'factory-staging']
    desired = set(args.group)

    apiurl = osc.conf.config['apiurl']
    devel_projects = devel_projects_load(args)
    for devel_project in devel_projects:
        meta = ET.fromstringlist(show_project_meta(apiurl, devel_project))
        groups = meta.xpath('group[@role="maintainer"]/@groupid')
        intersection = set(groups).intersection(desired)
        if len(intersection) != len(desired):
            print(f"{devel_project} missing {', '.join(desired - intersection)}")


def notify(args):
    import smtplib
    apiurl = osc.conf.config['apiurl']

    # devel_projects_get() only works for Factory as such
    # devel_project_fallback() must be used on a per package basis.
    packages = args.packages
    if not packages:
        packages = package_list_kind_filtered(apiurl, args.project)
    maintainer_map = {}
    for package in packages:
        devel_project, devel_package = devel_project_fallback(apiurl, args.project, package)
        if devel_project and devel_package:
            devel_package_identifier = '/'.join([devel_project, devel_package])
            userids = maintainers_get(apiurl, devel_project, devel_package)
            for userid in userids:
                maintainer_map.setdefault(userid, set())
                maintainer_map[userid].add(devel_package_identifier)

    subject = f'Packages you maintain are present in {args.project}'
    for userid, package_identifiers in maintainer_map.items():
        email = entity_email(apiurl, userid)
        message = """This is a friendly reminder about your packages in {}.

Please verify that the included packages are working as intended and
have versions appropriate for a stable release. Changes may be submitted until
April 26th [at the latest].

Keep in mind that some packages may be shared with SUSE Linux
Enterprise. Concerns with those should be raised via Bugzilla.

Please contact opensuse-releaseteam@opensuse.org if your package
needs special attention by the release team.

According to the information in OBS ("osc maintainer") you are
in charge of the following packages:

- {}""".format(
            args.project, '\n- '.join(sorted(package_identifiers)))

        log = f'notified {userid} of {len(package_identifiers)} packages'
        try:
            mail_send(apiurl, args.project, email, subject, message, dry=args.dry)
            print(log)
        except smtplib.SMTPRecipientsRefused:
            print(f'[FAILED ADDRESS] {log} ({email})')
        except smtplib.SMTPException as e:
            print(f'[FAILED SMTP] {log} ({e})')


def requests(args):
    apiurl = osc.conf.config['apiurl']
    devel_projects = devel_projects_load(args)

    # Disable including source project in get_request_list() query.
    osc.conf.config['include_request_from_project'] = False
    for devel_project in devel_projects:
        requests = get_request_list_with_history(
            apiurl, devel_project, req_state=('new', 'review'),
            req_type='submit')
        for request in requests:
            action = request.actions[0]
            age = request_age(request).days
            if age < args.min_age:
                continue

            print(
                ' '.join(
                    (
                        request.reqid,
                        '/'.join((action.tgt_project, action.tgt_package)),
                        '/'.join((action.src_project, action.src_package)),
                        f'({age} days old)',
                    )
                )
            )

            if args.remind:
                remind_comment(apiurl, args.repeat_age, request.reqid, action.tgt_project, action.tgt_package)


def reviews(args):
    apiurl = osc.conf.config['apiurl']
    devel_projects = devel_projects_load(args)

    for devel_project in devel_projects:
        requests = get_review_list(apiurl, byproject=devel_project)
        for request in requests:
            # get_review_list() behavior has been changed in osc
            # https://github.com/openSUSE/osc/commit/00decd25d1a2c775e455f8865359e0d21872a0a5
            if request.state.name != 'review':
                continue
            action = request.actions[0]
            if action.type != 'submit':
                continue

            age = request_age(request).days
            if age < args.min_age:
                continue

            for review in request.reviews:
                if review.by_project == devel_project:
                    break

            print(
                ' '.join(
                    (
                        request.reqid,
                        '/'.join((review.by_project, review.by_package))
                        if review.by_package
                        else review.by_project,
                        '/'.join((action.tgt_project, action.tgt_package)),
                        f'({age} days old)',
                    )
                )
            )

            if args.remind:
                remind_comment(apiurl, args.repeat_age, request.reqid, review.by_project, review.by_package)


def maintainers_get(apiurl, project, package=None):
    if package:
        try:
            meta = show_package_meta(apiurl, project, package)
        except HTTPError as e:
            if e.code == 404:
                # Fallback to project in the case of new package.
                meta = show_project_meta(apiurl, project)
    else:
        meta = show_project_meta(apiurl, project)
    meta = ET.fromstringlist(meta)

    userids = [
        person.get('userid')
        for person in meta.findall('person[@role="maintainer"]')
    ]
    if not userids and package is not None:
        # Fallback to project if package has no maintainers.
        return maintainers_get(apiurl, project)

    return userids


def remind_comment(apiurl, repeat_age, request_id, project, package=None):
    comment_api = CommentAPI(apiurl)
    comments = comment_api.get_comments(request_id=request_id)
    comment, _ = comment_api.comment_find(comments, BOT_NAME)

    if comment:
        delta = datetime.utcnow() - comment['when']
        if delta.days < repeat_age:
            print(f'  skipping due to previous reminder from {delta.days} days ago')
            return

        # Repeat notification so remove old comment.
        try:
            comment_api.delete(comment['id'])
        except HTTPError as e:
            if e.code == 403:
                # Gracefully skip when previous reminder was by another user.
                print('  unable to remove previous reminder')
                return
            raise e

    userids = sorted(maintainers_get(apiurl, project, package))
    if len(userids):
        users = [f'@{userid}' for userid in userids]
        message = f"{', '.join(users)}: {REMINDER}"
    else:
        message = REMINDER
    print(f'  {message}')
    message = comment_api.add_marker(message, BOT_NAME)
    comment_api.add_comment(request_id=request_id, comment=message)


def common_args_add(parser):
    parser.add_argument('--min-age', type=int, default=0, metavar='DAYS', help='min age of requests')
    parser.add_argument('--repeat-age', type=int, default=7, metavar='DAYS',
                        help='age after which a new reminder will be sent')
    parser.add_argument('--remind', action='store_true', help='remind maintainers to review')


def main():
    parser = argparse.ArgumentParser(description='Operate on devel projects for a given project.')
    subparsers = parser.add_subparsers(title='subcommands')

    parser.add_argument('-A', '--apiurl', metavar='URL', help='API URL')
    parser.add_argument('-d', '--debug', action='store_true', help='print info useful for debuging')
    parser.add_argument('-p', '--project', default='openSUSE:Factory', metavar='PROJECT',
                        help='project from which to source devel projects')

    parser_list = subparsers.add_parser('list', help='List devel projects.')
    parser_list.set_defaults(func=list)
    parser_list.add_argument('-w', '--write', action='store_true', help='write to pseudometa package')

    parser_maintainer = subparsers.add_parser('maintainer', help='Check for relevant groups as maintainer.')
    parser_maintainer.set_defaults(func=maintainer)
    parser_maintainer.add_argument('-g', '--group', action='append', help='group for which to check')

    parser_notify = subparsers.add_parser('notify', help='notify maintainers of their packages')
    parser_notify.set_defaults(func=notify)
    parser_notify.add_argument('--dry', action='store_true', help='dry run emails')
    parser_notify.add_argument("packages", nargs='*', help="packages to check")

    parser_requests = subparsers.add_parser('requests', help='List open requests.')
    parser_requests.set_defaults(func=requests)
    common_args_add(parser_requests)

    parser_reviews = subparsers.add_parser('reviews', help='List open reviews.')
    parser_reviews.set_defaults(func=reviews)
    common_args_add(parser_reviews)

    if not sys.argv[1:]:
        sys.argv.append("list")
    args = parser.parse_args()
    osc.conf.get_config(override_apiurl=args.apiurl)
    osc.conf.config['debug'] = args.debug
    sys.exit(args.func(args))


if __name__ == '__main__':
    main()
