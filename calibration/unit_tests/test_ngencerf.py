import json
from pathlib import Path

from django.conf import settings
from django.contrib.auth.models import User
from django.core.management import call_command
from django.db.models import Value, CharField
from django.db.models.functions import Concat
from django.forms import CharField
from django.test import TestCase
from rest_framework.test import APIClient, force_authenticate, APIRequestFactory

import calibration.views.calibration_landing_views
from calibration.enums import StatusEnum
from calibration.models.plot_definitions import PlotDefinition
from calibration.models.status import Status
from calibration.views import calibration_plot_views
from calibration.views.common import get_calibration_run


class CerfUnitTest(TestCase):
    """
    This function initializes the calibration database with a mockup test data in a
    calibration run record, and returns the run ID for the unit tests' use. It accomplishes
    this by importing Import_test_data/import_complete.json file.
    """

    # def setUp(self):
    @classmethod
    def setUpClass(self):
        super(CerfUnitTest, self).setUpClass()

        user = User.objects.create_user('admin', 'admin@...', 'admin')
        print(f"Username: {user.username}, email: {user.email}")
        # initialize DB static tables (call init_sql and init_gages commands)
        from calibration.management.commands import init_sql
        cmd = init_sql.Command()
        call_command(cmd)
        from calibration.management.commands import init_gages
        cmd = init_gages.Command()
        call_command(cmd)

        factory = APIRequestFactory()
        # Opening import_complete.json file
        f = open(Path(settings.BASE_DIR) / 'Import_test_data/import_complete.json')
        # returns JSON object as a dictionary
        data = json.load(f)
        request = factory.post("/api/calibration/import/", data, format="json")
        force_authenticate(request, user=user)
        response = calibration.views.calibration_landing_views.import_job(request)
        response.render()
        res = json.loads(response.content)
        self.run_id = res["calibration_run_id"]
        print(f"Executing setUp(): Calibration run ID = {self.run_id}")
        # verify the record for calibration_run_id
        run, error_return = get_calibration_run(self.run_id, user)
        if error_return:
            return error_return
        status = Status.objects.get(name=StatusEnum.RUNNING.value)
        run.status = status
        run.save()

    # Tests the /api/calibration/get_plot_names/  end-point
    def test_plot_definitions_endpoint(self):
        calibration_run_id = self.run_id
        print(f"Executing test_plot_definitions_endpoint(): Calibration run ID = {self.run_id}")
        client = APIClient()
        user = User.objects.get(username='admin')
        client.force_authenticate(user=user)
        response = client.get(
            f"/api/calibration/get_plot_names/?calibration_run_id={calibration_run_id}"
        )
        # check if transaction was successful
        self.assertEqual(response.status_code, 200)

        # assemble the expected response data
        run, error_return = get_calibration_run(calibration_run_id, user, run_status=[StatusEnum.RUNNING])
        if error_return:
            return error_return

        gage_id = run.gage.gage_id
        print(f"test_plot_definitions_view(): Gage ID: {gage_id}")
        plots = (
            PlotDefinition.objects.filter(is_active=True)
            .annotate(filename=Concat(Value(gage_id), 'filename_mask', output_field=CharField()))
            .values('name', 'description', 'filename')
        )
        expected_response = {f"calibration_run_id": calibration_run_id, "plot_list": list(plots)}

        self.assertEqual(json.loads(response.content), expected_response)
        print("test_plot_definitions_endpoint() Passed!!")

    # Tests directly the calibration_plot_views.get_plot_names() view
    def test_plot_definitions_view(self):
        calibration_run_id = self.run_id
        print(f"Executing test_plot_definitions_view(): Calibration run ID = {self.run_id}")
        factory = APIRequestFactory()
        user = User.objects.get(username='admin')
        if not user:
            user = User.objects.create_user('admin', 'test@...', 'tester')
        request = factory.get(
            f"/api/calibration/get_plot_names/?calibration_run_id={calibration_run_id}"
        )
        force_authenticate(request, user=user)
        response = calibration_plot_views.get_plot_names(request)
        response.render()
        # check if transaction was successful
        self.assertEqual(response.status_code, 200)

        run, error_return = get_calibration_run(calibration_run_id, request.user, run_status=[StatusEnum.RUNNING])
        if error_return:
            return error_return

        gage_id = run.gage.gage_id
        print(f"test_plot_definitions_view(): Gage ID: {gage_id}")
        plots = (
            PlotDefinition.objects.filter(is_active=True)
            .annotate(filename=Concat(Value(gage_id), 'filename_mask', output_field=CharField()))
            .values('name', 'description', 'filename')
        )
        expected_response = {f"calibration_run_id": calibration_run_id, "plot_list": list(plots)}

        # verify content
        self.maxDiff = None
        self.assertEqual(json.loads(response.content), expected_response)
        print("test_plot_definitions_view() Passed!!")
