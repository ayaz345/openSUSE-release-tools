#!/usr/bin/python3

from configparser import ConfigParser
from xdg.BaseDirectory import load_first_config
from lxml import etree as ET

import sys
import cmdln
import os

import osc.core
import ToolBase


class Requestfinder(ToolBase.ToolBase):

    def __init__(self):
        ToolBase.ToolBase.__init__(self)

    def fill_package_meta(self, project):
        self.package_metas = {}
        url = osc.core.makeurl(
            self.apiurl, ['search', 'package'], f"match=[@project='{project}']"
        )
        root = ET.fromstring(self.cached_GET(url))
        for p in root.findall('package'):
            name = p.attrib['name']
            self.package_metas[name] = p

    def find_requests(self, settings):
        xquery = settings['query']

        if settings['devel']:
            self.fill_package_meta('openSUSE:Factory')

        url = osc.core.makeurl(self.apiurl, ('search', 'request'), {"match": xquery})
        root = ET.parse(osc.core.http_GET(url)).getroot()

        self.requests = []

        for request in root.findall('request'):
            req = osc.core.Request()
            req.read(request)
            if settings['devel']:
                p = req.actions[0].tgt_package
                pm = self.package_metas[p] if p in self.package_metas else None
                devel = pm.find('devel') if pm else None
                if devel is None or devel.get('project') in settings['devel']:
                    self.requests.append(req)
            else:
                self.requests.append(req)

        return self.requests


class CommandLineInterface(ToolBase.CommandLineInterface):

    def __init__(self, *args, **kwargs):
        ToolBase.CommandLineInterface.__init__(self, args, kwargs)

        self.cp = ConfigParser()
        if d := load_first_config('opensuse-release-tools'):
            self.cp.read(os.path.join(d, 'requestfinder.conf'))

    def get_optparser(self):
        return ToolBase.CommandLineInterface.get_optparser(self)

    def setup_tool(self):
        return Requestfinder()

    def _load_settings(self, settings, name):
        section = f'settings {name}'
        for option in settings.keys():
            if self.cp.has_option(section, option):
                settings[option] = self.cp.get(section, option).replace('\n', ' ')

    def print_actions(self, r):
        for a in r.actions:
            if a.type == 'submit':
                print(' '.join(('#', r.reqid, a.type, a.src_project, a.src_package, a.tgt_project)))
            elif hasattr(a, 'tgt_package'):
                print(' '. join(('#', r.reqid, a.type, a.tgt_project, a.tgt_package)))
            else:
                print(' '. join(('#', r.reqid, a.type, a.tgt_project)))

    @cmdln.option('--exclude-project', metavar='PROJECT', action='append', help='exclude review by specific project')
    @cmdln.option('--exclude-user', metavar='USER', action='append', help='exclude review by specific user')
    @cmdln.option('--query', metavar='filterstr', help='filter string')
    @cmdln.option('--action', metavar='action', help='action (accept/decline)')
    @cmdln.option('--settings', metavar='settings', help='settings to load from config file')
    @cmdln.option('-m', '--message', metavar="message", help="message")
    @cmdln.option('--devel', dest='devel', metavar='PROJECT', action='append',
                  help='only packages with specified devel project')
    def do_review(self, subcmd, opts):
        """${cmd_name}: print commands for reviews

        ${cmd_usage}
        ${cmd_option_list}
        """

        settings = {
            'action': 'accept',
            'message': 'ok',
            'query': None,
            'exclude-project': None,
            'exclude-user': None,
            'exclude-group': None,
            'devel': None,
        }

        if opts.settings:
            self._load_settings(settings, opts.settings)

        if opts.action:
            settings['action'] = opts.action
            settings['message'] = opts.action

        if opts.message:
            settings['message'] = opts.message

        if opts.devel:
            settings['devel'] = opts.devel

        if opts.query:
            settings['query'] = opts.query

        if not settings['query']:
            raise Exception('please specify query')

        rqs = self.tool.find_requests(settings)
        for r in rqs:
            self.print_actions(r)
            for review in r.reviews:
                if review.state != 'new':
                    continue

                if review.by_project:
                    skip = False
                    if settings['exclude-project']:
                        for p in settings['exclude-project'].split(' '):
                            if review.by_project.startswith(p):
                                skip = True
                                break
                    if not skip:
                        if review.by_package:
                            print(
                                f"osc review {settings['action']} -m '{settings['message']}' -P {review.by_project} -p {review.by_package} {r.reqid}"
                            )
                        else:
                            print(
                                f"osc review {settings['action']} -m '{settings['message']}' -P {review.by_project} {r.reqid}"
                            )
                elif review.by_group:
                    skip = False
                    if settings['exclude-group']:
                        groups = settings['exclude-group'].split(' ')
                        for g in groups:
                            if review.by_group == g:
                                skip = True
                                break
                    if not skip:
                        print(
                            f"osc review {settings['action']} -m '{settings['message']}' -G {review.by_group} {r.reqid}"
                        )
                elif review.by_user:
                    skip = False
                    if settings['exclude-user']:
                        users = settings['exclude-user'].split(' ')
                        for u in users:
                            if review.by_user == u:
                                skip = True
                                break
                    if not skip:
                        print(
                            f"osc review {settings['action']} -m '{settings['message']}' -U {review.by_user} {r.reqid}"
                        )

    @cmdln.option('--query', metavar='filterstr', help='filter string')
    @cmdln.option('--action', metavar='action', help='action (accept/decline)')
    @cmdln.option('--settings', metavar='settings', help='settings to load from config file')
    @cmdln.option('-m', '--message', metavar="message", help="message")
    @cmdln.option('--devel', dest='devel', metavar='PROJECT', action='append', help='only packages with specified devel project')
    def do_request(self, subcmd, opts):
        """${cmd_name}: print commands for requests

        ${cmd_usage}
        ${cmd_option_list}
        """

        settings = {
            'action': 'reopen',
            'message': 'reopen',
            'query': None,
            'devel': None,
        }

        if opts.settings:
            self._load_settings(settings, opts.settings)

        if opts.action:
            settings['action'] = opts.action
            settings['message'] = opts.action

        if opts.message:
            settings['message'] = opts.message

        if opts.devel:
            settings['devel'] = opts.devel

        rqs = self.tool.find_requests(settings)
        for r in rqs:
            self.print_actions(r)
            print(f"osc rq {settings['action']} -m '{settings['message']}' {r.reqid}")

    def help_examples(self):
        return """$ cat > ~/.config/opensuse-release-tools/requestfinder.conf << EOF
        [settings foo]
        query = (review[@by_project='example' and @state='new']
                 and state/@name='review'
                 and action/source/@project='openSUSE:Factory'
                 and action/target/@project='openSUSE:Leap:15.0')
        exclude-user = repo-checker
        exclude-project = openSUSE:Leap:15.0:Staging
        message = override
        action = accept
        EOF
        $ ${name} review --settings foo | tee doit.sh
        ./doit.sh
        """


if __name__ == "__main__":
    app = CommandLineInterface()
    sys.exit(app.main())
