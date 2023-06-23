from lxml import etree as ET

from osc import oscerr
from osc.core import http_GET

from osclib.request_finder import RequestFinder
from osclib.freeze_command import MAX_FROZEN_AGE

SELECT = 'select'
# SUPERSEDE = 'supersede'
MOVE = 'move'


class SelectCommand(object):

    def __init__(self, api, target_project):
        self.api = api
        self.affected_projects = set()
        self.target_project = target_project

    def _package(self, request):
        """
        Get the package name from the submit request XML.
        :param request: request we check for
        """
        f = http_GET(self.api.makeurl(['request', str(request)]))
        root = ET.parse(f).getroot()
        return str(root.find('action').find('target').attrib['package'])

    def _supersede(self, request):
        """
        Check if the request supersede a different request from a
        staging project.

        SRA supersede SRB when (1) SRA ID > SRB ID and (2) the changes
        in SRB are in SRA. The second condition is difficult to
        assure, but the way that we implement RequestFinder can
        address some corner cases that make the first condition
        enough.

        :param request: request we check for
        """
        package = self._package(request)

        candidates = []   # Store candidates to be supersede by 'request'
        url = self.api.makeurl(['staging', self.api.project, 'staging_projects'], {'requests': 1})
        status = ET.parse(self.api.retried_GET(url)).getroot()
        for prj in status.findall('staging_project'):
            candidates.extend(
                (req.get('id'), package, prj.get('name'))
                for req in prj.findall('./staged_requests/request')
                if int(req.get('id')) < int(request)
                and req.get('package') == package
            )
        assert (
            len(candidates) <= 1
        ), f'There are more than one candidate to supersede {request} ({package}): {candidates}'

        return candidates[0] if candidates else None

    def select_request(self, request, move, filter_from, remove_exclusion=False):
        staged_requests = {
            int(self.api.packages_staged[package]['rq_id']): package for package in self.api.packages_staged
        }
        supersede = self._supersede(request) if request in staged_requests else False
        if request not in staged_requests and not supersede:
            # Normal 'select' command
            print(f'Adding request "{request}" to project "{self.target_project}"')

            return self.api.rq_to_prj(request, self.target_project, remove_exclusion)
        elif request in staged_requests and (move or supersede):
            # 'select' command becomes a 'move'
            # supersede = (new_rq, package, project)
            fprj = self.api.packages_staged[staged_requests[request]]['prj'] if not supersede else supersede[2]
            if filter_from and filter_from != fprj:
                print(f'Ignoring "{request}" in "{fprj}" since not in "{filter_from}"')
                return True

            if supersede:
                print(f'"{request} ({supersede[1]}) is superseded by {supersede[0]}')

            if fprj == self.target_project:
                print(f'"{request}" is currently in "{self.target_project}"')
                return False

            print(f'Moving "{request}" from "{fprj}" to "{self.target_project}"')

            # Store the source project, we also need to write a comment there
            self.affected_projects.add(fprj)

            return self.api.move_between_project(fprj, request, self.target_project)
        elif request in staged_requests:
            # Previously selected, but not explicit move
            fprj = self.api.packages_staged[staged_requests[request]]['prj']
            msg = 'Request {} is already tracked in "{}".'
            msg = msg.format(request, fprj)
            if fprj != self.target_project:
                msg += '\nUse --move modifier to move the request from "{}" to "{}"'
                msg = msg.format(fprj, self.target_project)
            print(msg)
            return True
        else:
            print(f'"{request} ({supersede[1]}) supersedes {supersede[0]}')

    def perform(self, requests, move=False,
                filter_from=None, no_freeze=False, remove_exclusion=False):
        """
        Select package and move it accordingly by arguments
        :param target_project: project we want to target
        :param requests: requests we are working with
        :param move: wether to move the requests or not
        :param filter_from: filter request list to only those from a specific staging
        """

        if self.api.is_adi_project(self.target_project):
            no_freeze = True

        # If the project is not frozen enough yet freeze it
        if not (no_freeze or self.api.prj_frozen_enough(self.target_project)):
            print('Project needs to be frozen or there was no change for last %d days.' % MAX_FROZEN_AGE)
            print('Please freeze the project or use an option to ignore the time from the last freee.')
            return False

        # picks new candidate requests only if it's not to move requests
        # ie. the review state of staging-project must be new if newcand is True
        newcand = not move

        requests = RequestFinder.find_sr(requests, self.api, newcand, consider_stagings=move)
        requests_count = len(requests)
        for index, request in enumerate(requests, start=1):
            print(f'({index}/{requests_count}) ', end='')
            if not self.select_request(request, move, filter_from, remove_exclusion=remove_exclusion):
                return False

        # Notify everybody about the changes
        self.api.update_status_or_deactivate(self.target_project, 'select')
        for fprj in self.affected_projects:
            self.api.update_status_or_deactivate(fprj, 'select')

        return True
