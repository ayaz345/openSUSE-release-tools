#!/usr/bin/python3

import argparse
import bugzilla
import dateutil.parser
from datetime import datetime
from dateutil.tz import tzlocal
import os
from random import shuffle
import subprocess
import sys
import tempfile
from xmlrpclib import Fault
import yaml
from lxml import etree as ET

import osc.conf
import osc.core

from osclib.cache import Cache
from osclib.core import entity_email
from osclib.core import package_list
from osclib.git import CACHE_DIR
from osclib.git import sync

# Issue summary can contain unicode characters and therefore a string containing
# either summary or one in which ISSUE_SUMMARY is then placed must be unicode.
# For example, translation-update-upstream contains bsc#877707 which has a
# unicode character in its summary.
BUG_SUMMARY = '[patch-lost-in-sle] Missing issues in {factory}/{package}'
BUG_TEMPLATE = u'{message_start}\n\n{issues}'
MESSAGE_START = """The following issues were referenced in the changelog for {project}/{package},
but where not found in {factory}/{package} after {newest} days. Review the issues and submit changes
to {factory} to ensure all relevant changes end up in {factory} which is used as the basis for the next SLE version.
For more information and details on how to go about submitting the changes
see https://mailman.suse.de/mlarch/SuSE/research/2017/research.2017.02/msg00051.html."""
ISSUE_SUMMARY = u'[{label}]({url}) owned by {owner}: {summary}'
ISSUE_SUMMARY_BUGZILLA = u'{label} owned by {owner}: {summary}'
ISSUE_SUMMARY_PLAIN = u'[{label}]({url})'
ISSUE_SUMMARY_PLAIN_BUGZILLA = u'{label}'


def bug_create(bugzilla_api, meta, assigned_to, cc, summary, description):
    createinfo = bugzilla_api.build_createbug(
        product=meta[0],
        component=meta[1],
        version=meta[2],
        severity='normal',
        op_sys='Linux',
        platform='PC',
        assigned_to=assigned_to,
        cc=cc,
        summary=summary,
        description=description)
    newbug = bugzilla_api.createbug(createinfo)

    return newbug.id


def bug_owner(apiurl, package, entity='person'):
    query = {
        'binary': package,
    }
    url = osc.core.makeurl(apiurl, ('search', 'owner'), query=query)
    root = ET.parse(osc.core.http_GET(url)).getroot()

    bugowner = root.find(f'.//{entity}[@role="bugowner"]')
    if bugowner is not None:
        return entity_email(apiurl, bugowner.get('name'), entity)
    maintainer = root.find(f'.//{entity}[@role="maintainer"]')
    if maintainer is not None:
        return entity_email(apiurl, maintainer.get('name'), entity)
    return bug_owner(apiurl, package, 'group') if entity == 'person' else None


def bug_meta_get(bugzilla_api, bug_id):
    try:
        bug = bugzilla_api.getbug(bug_id)
    except Fault as e:
        print(f'bug_meta_get(): {str(e)}')
        return None
    return bug.component


def bug_meta(bugzilla_api, defaults, trackers, issues):
    # Extract meta from the first bug from bnc tracker or fallback to defaults.
    prefix = trackers['bnc'][:3]
    for issue in issues:
        if issue.startswith(prefix):
            if component := bug_meta_get(bugzilla_api, issue[4:]):
                return (defaults[0], component, defaults[2])

    return defaults


def bugzilla_init(apiurl):
    bugzilla_api = bugzilla.Bugzilla(apiurl)
    if not bugzilla_api.logged_in:
        print('Bugzilla credentials required to create bugs.')
        bugzilla_api.interactive_login()
    return bugzilla_api


def prompt_continue(change_count):
    allowed = ['y', 'b', 's', 'n', '']
    if change_count > 0:
        print(
            f'File bug for {change_count} issues and continue? [y/b/s/n/?] (y): ',
            end='',
        )
    else:
        print('No changes for which to create bug, continue? [y/b/s/n/?] (y): ', end='')

    response = input().lower()
    if response == '?':
        print('b = break; file bug if applicable, record in db, and stop\ns = skip package')
    elif response in allowed:
        if response == '':
            response = 'y'
        return response
    else:
        print(f'Invalid response: {response}')

    return prompt_continue(change_count)


def prompt_interactive(changes, project, package):
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yml') as temp:
        temp.write(yaml.safe_dump(changes, default_flow_style=False, default_style="'") + '\n')
        temp.write(f'# {project}/{package}\n')
        temp.write('# comment or remove lines to whitelist issues')
        temp.flush()

        editor = os.getenv('EDITOR')
        if not editor:
            editor = 'xdg-open'
        subprocess.call(editor.split(' ') + [temp.name])

        changes_after = yaml.safe_load(open(temp.name).read())
        if changes_after is None:
            changes_after = {}

        return changes_after


def issue_found(package, label, db):
    return package in db and db[package] is not None and label in db[package]


def issue_trackers(apiurl):
    url = osc.core.makeurl(apiurl, ['issue_trackers'])
    root = ET.parse(osc.core.http_GET(url)).getroot()
    return {
        tracker.find('name').text: tracker.find('label').text
        for tracker in root.findall('issue-tracker')
    }


def issue_normalize(trackers, tracker, name):
    if tracker in trackers:
        return trackers[tracker].replace('@@@', name)

    print(f'WARNING: ignoring unknown tracker {tracker} for {name}')
    return None


def issues_get(apiurl, project, package, trackers, db):
    issues = {}

    url = osc.core.makeurl(apiurl, ['source', project, package], {'view': 'issues'})
    root = ET.parse(osc.core.http_GET(url)).getroot()

    now = datetime.now(tzlocal())  # Much harder than should be.
    for issue in root.findall('issue'):
        # Normalize issues to active API instance issue-tracker definitions.
        # Assumes the two servers have the name trackers, but different labels.
        label = issue_normalize(trackers, issue.find('tracker').text, issue.find('name').text)
        if label is None:
            continue

        # Ignore already processed issues.
        if issue_found(package, label, db):
            continue

        summary = issue.find('summary')
        if summary is not None:
            summary = summary.text

        owner = issue.find('owner/email')
        if owner is not None:
            owner = owner.text

        created = issue.find('created_at')
        updated = issue.find('updated_at')
        if created is not None and created.text is not None:
            date = created.text
        elif updated is not None and updated.text is not None:
            date = updated.text
        else:
            # Old date to make logic work.
            date = '2007-12-12 00:00 GMT+1'

        date = dateutil.parser.parse(date)
        delta = now - date

        issues[label] = {
            'url': issue.find('url').text,
            'summary': summary,
            'owner': owner,
            'age': delta.days,
        }

    return issues


def print_stats(db):
    bug_ids = []
    reported = 0
    whitelisted = 0
    for package, bugs in db.items():
        if bugs == 'whitelist':
            continue
        for reference, outcome in bugs.items():
            if outcome != 'whitelist':
                bug_ids.append(int(outcome))
                reported += 1
            else:
                whitelisted += 1
    print(f'Packages: {len(db)}')
    print(f'Bugs: {len(set(bug_ids))}')
    print(f'Reported: {reported}')
    print(f'Whitelisted: {whitelisted}')


def main(args):
    # Store the default apiurl in addition to the overriden url if the
    # option was set and thus overrides the default config value.
    # Using the OBS link does not work for ?view=issues.
    if args.apiurl is not None:
        osc.conf.get_config()
        apiurl_default = osc.conf.config['apiurl']
    else:
        apiurl_default = None

    osc.conf.get_config(override_apiurl=args.apiurl)
    osc.conf.config['debug'] = args.debug
    apiurl = osc.conf.config['apiurl']

    Cache.init()

    git_repo_url = 'git@github.com:jberry-suse/openSUSE-release-tools-issue-db.git'
    git_message = 'Sync issue-diff.py changes.'
    db_dir = sync(args.cache_dir, git_repo_url, git_message)
    db_file = os.path.join(db_dir, f'{args.project}.yml')

    if os.path.exists(db_file):
        db = yaml.safe_load(open(db_file).read())
        if db is None:
            db = {}
        else:
            print(f'Loaded db file: {db_file}')
    else:
        db = {}

    if args.print_stats:
        print_stats(db)
        return

    print(f'Comparing {args.project} against {args.factory}')

    bugzilla_api = bugzilla_init(args.bugzilla_apiurl)
    bugzilla_defaults = (args.bugzilla_product, args.bugzilla_component, args.bugzilla_version)

    trackers = issue_trackers(apiurl)
    packages_project = package_list(apiurl, args.project)
    packages_factory = package_list(apiurl_default, args.factory)
    packages = set(packages_project).intersection(set(packages_factory))
    new = 0
    shuffle(list(packages))
    for index, package in enumerate(packages, start=1):
        if index % 50 == 0:
            print(f'Checked {index} of {len(packages)}')
        if package in db and db[package] == 'whitelist':
            print(f'Skipping package {package}')
            continue

        issues_project = issues_get(apiurl, args.project, package, trackers, db)
        issues_factory = issues_get(apiurl_default, args.factory, package, trackers, db)

        missing_from_factory = set(issues_project.keys()) - set(issues_factory.keys())

        # Filtering by age must be done after set diff in order to allow for
        # matches with issues newer than --newest.
        for label in set(missing_from_factory):
            if issues_project[label]['age'] < args.newest:
                missing_from_factory.remove(label)

        if len(missing_from_factory) == 0:
            continue

        print(f'{package}: {len(missing_from_factory)} missing')

        # Generate summaries for issues missing from factory.
        changes = {}
        for issue in missing_from_factory:
            info = issues_project[issue]
            summary = ISSUE_SUMMARY if info['owner'] else ISSUE_SUMMARY_PLAIN
            changes[issue] = summary.format(
                label=issue, url=info['url'], owner=info['owner'], summary=info['summary'])

        # Prompt user to decide which issues to whitelist.
        changes_after = prompt_interactive(changes, args.project, package)

        # Determine if any real changes (vs typos) and create text issue list.
        issues = []
        cc = []
        if len(changes_after) > 0:
            for issue, summary in changes.items():
                if issue in changes_after:
                    info = issues_project[issue]
                    if issue.startswith('bsc'):
                        # Reformat for bugzilla markdown.
                        summary = ISSUE_SUMMARY_BUGZILLA if info['owner'] else ISSUE_SUMMARY_PLAIN_BUGZILLA
                        issue = issue.replace('bsc', 'bug')
                        summary = summary.format(
                            label=issue, url=info['url'], owner=info['owner'], summary=info['summary'])
                    issues.append(f'- {summary}')
                    if info['owner'] is not None:
                        cc.append(info['owner'])

        # Prompt user about how to continue.
        response = prompt_continue(len(issues))
        if response == 'n':
            break
        if response == 's':
            continue

        # File a bug if not all issues whitelisted.
        if issues:
            summary = BUG_SUMMARY.format(project=args.project, factory=args.factory, package=package)
            message = BUG_TEMPLATE.format(
                message_start=MESSAGE_START.format(
                    project=args.project, factory=args.factory, package=package, newest=args.newest),
                issues='\n'.join(issues))
            if len(message) > 65535:
                # Truncate messages longer than bugzilla limit.
                message = f'{message[:65535 - 3]}...'

            # Determine bugzilla meta information to use when creating bug.
            meta = bug_meta(bugzilla_api, bugzilla_defaults, trackers, changes.keys())
            owner = bug_owner(apiurl, package)
            if args.bugzilla_cc:
                cc.append(args.bugzilla_cc)

            # Try to create bug, but allow for handling faults.
            tries = 0
            while tries < 10:
                try:
                    bug_id = bug_create(bugzilla_api, meta, owner, cc, summary, message)
                    break
                except Fault as e:
                    if 'There is no component named' in e.faultString:
                        print(f'Invalid component {meta[1]}, fallback to default')
                        meta = (meta[0], bugzilla_defaults[1], meta[2])
                    elif 'is not a valid username' in e.faultString:
                        username = e.faultString.split(' ', 3)[2]
                        cc.remove(username)
                        print(f'Removed invalid username {username}')
                    else:
                        raise e
                tries += 1

        # Mark changes in db.
        notified, whitelisted = 0, 0
        for issue in changes:
            if package not in db:
                db[package] = {}

            if issue in changes_after:
                db[package][issue] = str(bug_id)
                notified += 1
            else:
                db[package][issue] = 'whitelist'
                whitelisted += 1

        # Write out changes after each package to avoid loss.
        with open(db_file, 'w') as outfile:
            yaml.safe_dump(db, outfile, default_flow_style=False, default_style="'")

        if notified > 0:
            print(
                f'{package}: {notified} notified in bug {bug_id}, {whitelisted} whitelisted'
            )
        else:
            print(f'{package}: {whitelisted} whitelisted')

        if response == 'b':
            break

        new += 1
        if new == args.limit:
            print('stopped at limit')
            break

    sync(args.cache_dir, git_repo_url, git_message)


if __name__ == '__main__':
    description = 'Compare packages from a project against factory for differences in referenced issues and ' \
                  'present changes to allow whitelisting before creating bugzilla entries. A database is kept ' \
                  'of previously handled issues to avoid repeats and kept in sync via a git repository.'
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument('-A', '--apiurl', default='https://api.suse.de', metavar='URL', help='OBS instance API URL')
    parser.add_argument('--bugzilla-apiurl', required=True, metavar='URL', help='bugzilla API URL')
    parser.add_argument('--bugzilla-product', default='SUSE Linux Enterprise Server 15',
                        metavar='PRODUCT', help='default bugzilla product')
    parser.add_argument('--bugzilla-component', default='Other', metavar='COMPONENT', help='default bugzilla component')
    parser.add_argument('--bugzilla-version', default='unspecified', metavar='VERSION', help='default bugzilla version')
    parser.add_argument('--bugzilla-cc', metavar='EMAIL', help='bugzilla address added to cc on all bugs created')
    parser.add_argument('-d', '--debug', action='store_true', help='print info useful for debugging')
    parser.add_argument('-f', '--factory', default='openSUSE:Factory', metavar='PROJECT',
                        help='factory project to use as baseline for comparison')
    parser.add_argument('-p', '--project', default='SUSE:SLE-12-SP3:GA', metavar='PROJECT',
                        help='project to check for issues that have are not found in factory')
    parser.add_argument('--newest', type=int, default='30', metavar='AGE_IN_DAYS',
                        help='newest issues to be considered')
    parser.add_argument('--limit', type=int, default='0', help='limit number of packages with new issues processed')
    parser.add_argument('--cache-dir', help='cache directory containing git-sync tool and issue db')
    parser.add_argument('--print-stats', action='store_true', help='print statistics based on database')
    args = parser.parse_args()

    if args.cache_dir is None:
        args.cache_dir = CACHE_DIR

    sys.exit(main(args))
