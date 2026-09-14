from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.integrations.google_maps import LocksmithDistance
from apps.integrations.handl import FutureLocksmithAttendance, JobDetails
from apps.locksmiths.models import Locksmith, SoterLocksmithId

from .templatetags.logs_engine_extras import drive_time_class
from .views import _straight_line_miles


def _job_with_location(**overrides):
    fields = dict(
        report_id="501179", make="Ford", model="Focus", year="2020",
        reg="AB20 CDE", vin="VIN1", service_type="Car", loss_type="LOST",
        supplied_service="", net_cost=100.0,
        vehicle_latitude=52.6309, vehicle_longitude=1.2974,
    )
    fields.update(overrides)
    return JobDetails(**fields)


class LogsEngineLookupTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="office", email="admin@wgtk.co.uk", password="x", is_staff=True
        )
        self.client.force_login(self.user)

    def test_login_required(self):
        self.client.logout()
        response = self.client.get(reverse("logs_engine:lookup"))
        self.assertEqual(response.status_code, 302)

    def test_no_report_id_shows_blank_form_only(self):
        response = self.client.get(reverse("logs_engine:lookup"))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["searched"])
        self.assertIsNone(response.context["job"])
        self.assertNotContains(response, "No job found")

    @patch("apps.logs_engine.views.get_handl_client")
    def test_found_report_id_shows_job_card(self, mock_get_handl):
        mock_get_handl.return_value = MagicMock(get_job_details=MagicMock(return_value={
            "501179": JobDetails(
                report_id="501179", make="Ford", model="Focus", year="2020",
                reg="AB20 CDE", vin="VIN1", service_type="Car", loss_type="LOST",
                supplied_service="Non-Destructive Entry", net_cost=100.0,
            ),
        }))

        response = self.client.get(reverse("logs_engine:lookup"), {"report_id": "501179"})

        self.assertEqual(response.status_code, 200)
        job = response.context["job"]
        self.assertEqual(job.reg, "AB20 CDE")
        self.assertEqual(job.make, "Ford")
        self.assertContains(response, "AB20 CDE")
        self.assertContains(response, "Ford Focus 2020")
        mock_get_handl.return_value.get_job_details.assert_called_once_with(["501179"])

    @patch("apps.logs_engine.views.get_handl_client")
    def test_unknown_report_id_shows_not_found(self, mock_get_handl):
        mock_get_handl.return_value = MagicMock(get_job_details=MagicMock(return_value={}))

        response = self.client.get(reverse("logs_engine:lookup"), {"report_id": "999999"})

        self.assertIsNone(response.context["job"])
        self.assertContains(response, "No job found for ReportID 999999")

    @patch("apps.logs_engine.views.get_handl_client")
    def test_handl_failure_shows_not_found_rather_than_erroring(self, mock_get_handl):
        mock_get_handl.return_value = MagicMock(
            get_job_details=MagicMock(side_effect=Exception("boom"))
        )

        response = self.client.get(reverse("logs_engine:lookup"), {"report_id": "501179"})

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context["job"])
        self.assertContains(response, "No job found for ReportID 501179")

    @patch("apps.logs_engine.views.get_handl_client")
    def test_strips_whitespace_from_report_id(self, mock_get_handl):
        mock_get_handl.return_value = MagicMock(get_job_details=MagicMock(return_value={
            "501179": JobDetails(
                report_id="501179", make="Ford", model="Focus", year="2020",
                reg="AB20 CDE", vin="VIN1", service_type="Car", loss_type="LOST",
                supplied_service="", net_cost=100.0,
            ),
        }))

        response = self.client.get(reverse("logs_engine:lookup"), {"report_id": "  501179  "})

        mock_get_handl.return_value.get_job_details.assert_called_once_with(["501179"])
        self.assertIsNotNone(response.context["job"])


class LogsEngineNearestLocksmithsTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="office", email="admin@wgtk.co.uk", password="x", is_staff=True
        )
        self.client.force_login(self.user)

    @patch("apps.logs_engine.views.get_google_maps_client")
    @patch("apps.logs_engine.views.get_handl_client")
    def test_ranks_locksmiths_nearest_first(self, mock_get_handl, mock_get_maps):
        mock_get_handl.return_value = MagicMock(
            get_job_details=MagicMock(return_value={"501179": _job_with_location()}),
            get_future_locksmith_attendances=MagicMock(return_value=[]),
        )
        far = Locksmith.objects.create(name="WGTK - Far Away", home_postcode="IP1 2AB")
        near = Locksmith.objects.create(name="WGTK - Nearby", home_postcode="NR14 8PL")
        mock_get_maps.return_value = MagicMock(get_distances=MagicMock(return_value=[
            LocksmithDistance(origin="IP1 2AB", distance_metres=40000.0, duration_seconds=3000, status="OK"),
            LocksmithDistance(origin="NR14 8PL", distance_metres=8369.0, duration_seconds=720, status="OK"),
        ]))

        response = self.client.get(reverse("logs_engine:lookup"), {"report_id": "501179"})

        nearest = response.context["nearest_locksmiths"]
        self.assertEqual([r.locksmith.pk for r in nearest], [near.pk, far.pk])
        self.assertContains(response, "WGTK - Nearby")
        self.assertContains(response, "WGTK - Far Away")
        # order in the passed-in list matters — this is how results get
        # zipped back onto the right locksmith
        call_origins = mock_get_maps.return_value.get_distances.call_args[0][0]
        self.assertEqual(call_origins, ["IP1 2AB", "NR14 8PL"])  # ordered by name, no future attendances

    @patch("apps.logs_engine.views.get_google_maps_client")
    @patch("apps.logs_engine.views.get_handl_client")
    def test_home_lat_lng_preferred_over_postcode_as_origin(self, mock_get_handl, mock_get_maps):
        # Optimo's "driver starting location" import (see
        # apps.locksmiths.services) gives a precise coordinate — more
        # accurate than a postcode's centroid, so it should be used
        # instead whenever it's set.
        Locksmith.objects.create(
            name="WGTK - Nearby", home_postcode="NR14 8PL",
            home_latitude=52.6075364, home_longitude=1.2922435,
        )
        mock_get_handl.return_value = MagicMock(
            get_job_details=MagicMock(return_value={"501179": _job_with_location()}),
            get_future_locksmith_attendances=MagicMock(return_value=[]),
        )
        mock_get_maps.return_value = MagicMock(get_distances=MagicMock(return_value=[
            LocksmithDistance(origin="52.6075364,1.2922435", distance_metres=8369.0, duration_seconds=720, status="OK"),
        ]))

        response = self.client.get(reverse("logs_engine:lookup"), {"report_id": "501179"})

        call_origins = mock_get_maps.return_value.get_distances.call_args[0][0]
        self.assertEqual(call_origins, ["52.6075364,1.2922435"])
        self.assertEqual(len(response.context["nearest_locksmiths"]), 1)

    @patch("apps.logs_engine.views.get_google_maps_client")
    @patch("apps.logs_engine.views.get_handl_client")
    def test_home_lat_lng_too_far_away_excluded_before_calling_google(self, mock_get_handl, mock_get_maps):
        # Job is near Norwich (see _job_with_location); this locksmith's
        # home is in Glasgow, ~350+ miles away as the crow flies — not a
        # sensible suggestion, and not worth a real API call confirming
        # that.
        Locksmith.objects.create(
            name="WGTK - Too Far", home_postcode="G1 1AA",
            home_latitude=55.8642, home_longitude=-4.2518,
        )
        mock_get_handl.return_value = MagicMock(
            get_job_details=MagicMock(return_value={"501179": _job_with_location()}),
            get_future_locksmith_attendances=MagicMock(return_value=[]),
        )
        mock_get_maps.return_value = MagicMock(get_distances=MagicMock(return_value=[]))

        response = self.client.get(reverse("logs_engine:lookup"), {"report_id": "501179"})

        mock_get_maps.return_value.get_distances.assert_not_called()
        self.assertEqual(response.context["nearest_locksmiths"], [])

    @patch("apps.logs_engine.views.get_google_maps_client")
    @patch("apps.logs_engine.views.get_handl_client")
    def test_postcode_only_home_never_distance_filtered(self, mock_get_handl, mock_get_maps):
        # No lat/lng on file means no straight-line distance to check —
        # always let a postcode-only home through rather than silently
        # dropping it.
        Locksmith.objects.create(name="WGTK - Postcode Only", home_postcode="G1 1AA")
        mock_get_handl.return_value = MagicMock(
            get_job_details=MagicMock(return_value={"501179": _job_with_location()}),
            get_future_locksmith_attendances=MagicMock(return_value=[]),
        )
        mock_get_maps.return_value = MagicMock(get_distances=MagicMock(return_value=[
            LocksmithDistance(origin="G1 1AA", distance_metres=90000.0, duration_seconds=5400, status="OK"),
        ]))

        response = self.client.get(reverse("logs_engine:lookup"), {"report_id": "501179"})

        call_origins = mock_get_maps.return_value.get_distances.call_args[0][0]
        self.assertEqual(call_origins, ["G1 1AA"])
        self.assertEqual(len(response.context["nearest_locksmiths"]), 1)

    @patch("apps.logs_engine.views.get_google_maps_client")
    @patch("apps.logs_engine.views.get_handl_client")
    def test_excludes_locksmiths_without_a_postcode_and_counts_them(self, mock_get_handl, mock_get_maps):
        mock_get_handl.return_value = MagicMock(get_job_details=MagicMock(
            return_value={"501179": _job_with_location()}
        ))
        Locksmith.objects.create(name="WGTK - No Postcode", home_postcode="")
        Locksmith.objects.create(name="WGTK - Inactive", home_postcode="NR14 8PL", active=False)
        mock_get_maps.return_value = MagicMock(get_distances=MagicMock(return_value=[]))

        response = self.client.get(reverse("logs_engine:lookup"), {"report_id": "501179"})

        self.assertEqual(response.context["nearest_locksmiths"], [])
        self.assertEqual(response.context["locksmiths_missing_postcode"], 1)
        self.assertContains(response, "1 active locksmith has no home postcode set")
        mock_get_maps.return_value.get_distances.assert_not_called()

    @patch("apps.logs_engine.views.get_google_maps_client")
    @patch("apps.logs_engine.views.get_handl_client")
    def test_locksmith_with_no_home_postcode_still_shown_via_future_job(self, mock_get_handl, mock_get_maps):
        # No home postcode on file at all shouldn't silently drop a
        # locksmith who already has an upcoming job right near this one.
        no_home_postcode = Locksmith.objects.create(name="WGTK - No Postcode", home_postcode="")
        SoterLocksmithId.objects.create(locksmith=no_home_postcode, soter_locksmith_id="1204")

        mock_get_handl.return_value = MagicMock(
            get_job_details=MagicMock(return_value={"501179": _job_with_location()}),
            get_future_locksmith_attendances=MagicMock(return_value=[
                FutureLocksmithAttendance(
                    report_id="502000",
                    soter_locksmith_id="1204",
                    locksmith_name="WGTK - No Postcode",
                    available_from=datetime.now() + timedelta(days=1),
                    vehicle_postcode="NR14 8PL",
                    vehicle_reg="AB20 CDE",
                ),
            ]),
        )
        mock_get_maps.return_value = MagicMock(get_distances=MagicMock(return_value=[
            LocksmithDistance(origin="NR14 8PL", distance_metres=8369.0, duration_seconds=720, status="OK"),
        ]))

        response = self.client.get(reverse("logs_engine:lookup"), {"report_id": "501179"})

        nearest = response.context["nearest_locksmiths"]
        self.assertEqual(len(nearest), 1)
        ranked = nearest[0]
        self.assertEqual(ranked.locksmith.pk, no_home_postcode.pk)
        self.assertIsNotNone(ranked.attendance)
        # No home location on file at all means no return-trip leg can
        # be resolved either — total_minutes degrades to None rather
        # than guessing, and get_distances is only ever called once
        # (for the outbound leg) since there's nowhere to look up a
        # return trip to.
        self.assertIsNone(ranked.total_minutes)
        mock_get_maps.return_value.get_distances.assert_called_once()
        call_origins = mock_get_maps.return_value.get_distances.call_args[0][0]
        self.assertEqual(call_origins, ["NR14 8PL"])  # only the future-job origin, no home postcode

    @patch("apps.logs_engine.views.get_handl_client")
    def test_no_vehicle_location_skips_distance_lookup(self, mock_get_handl):
        mock_get_handl.return_value = MagicMock(get_job_details=MagicMock(
            return_value={"501179": _job_with_location(vehicle_latitude=None, vehicle_longitude=None)}
        ))
        Locksmith.objects.create(name="WGTK - Nearby", home_postcode="NR14 8PL")

        with patch("apps.logs_engine.views.get_google_maps_client") as mock_get_maps:
            response = self.client.get(reverse("logs_engine:lookup"), {"report_id": "501179"})
            mock_get_maps.assert_not_called()

        self.assertEqual(response.context["nearest_locksmiths"], [])
        self.assertContains(response, "no vehicle location recorded")

    @patch("apps.logs_engine.views.get_google_maps_client")
    @patch("apps.logs_engine.views.get_handl_client")
    def test_google_failure_shows_no_suggestions_rather_than_erroring(self, mock_get_handl, mock_get_maps):
        mock_get_handl.return_value = MagicMock(get_job_details=MagicMock(
            return_value={"501179": _job_with_location()}
        ))
        Locksmith.objects.create(name="WGTK - Nearby", home_postcode="NR14 8PL")
        mock_get_maps.return_value = MagicMock(
            get_distances=MagicMock(side_effect=Exception("boom"))
        )

        response = self.client.get(reverse("logs_engine:lookup"), {"report_id": "501179"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["nearest_locksmiths"], [])

    @patch("apps.logs_engine.views.get_google_maps_client")
    @patch("apps.logs_engine.views.get_handl_client")
    def test_google_failure_surfaces_the_actual_error_to_the_page(self, mock_get_handl, mock_get_maps):
        # A silent empty result here is indistinguishable from "nobody
        # has a location set" — surfacing the real error (e.g. an API
        # key restriction) is what actually lets office staff (or
        # whoever's debugging) tell the two apart without server logs.
        mock_get_handl.return_value = MagicMock(get_job_details=MagicMock(
            return_value={"501179": _job_with_location()}
        ))
        Locksmith.objects.create(name="WGTK - Nearby", home_postcode="NR14 8PL")
        mock_get_maps.return_value = MagicMock(
            get_distances=MagicMock(side_effect=ValueError(
                "Distance Matrix request failed: REQUEST_DENIED — API key restricted"
            ))
        )

        response = self.client.get(reverse("logs_engine:lookup"), {"report_id": "501179"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["nearest_locksmiths"], [])
        self.assertEqual(
            response.context["nearest_locksmiths_error"],
            "Distance Matrix request failed: REQUEST_DENIED — API key restricted",
        )
        self.assertContains(response, "Distance lookup failed")
        self.assertContains(response, "REQUEST_DENIED")

    @patch("apps.logs_engine.views.get_google_maps_client")
    @patch("apps.logs_engine.views.get_handl_client")
    def test_unresolved_origin_excluded_from_ranked_list(self, mock_get_handl, mock_get_maps):
        mock_get_handl.return_value = MagicMock(get_job_details=MagicMock(
            return_value={"501179": _job_with_location()}
        ))
        Locksmith.objects.create(name="WGTK - Bad Postcode", home_postcode="NOTREAL")
        mock_get_maps.return_value = MagicMock(get_distances=MagicMock(return_value=[
            LocksmithDistance(origin="NOTREAL", distance_metres=None, duration_seconds=None, status="NOT_FOUND"),
        ]))

        response = self.client.get(reverse("logs_engine:lookup"), {"report_id": "501179"})

        self.assertEqual(response.context["nearest_locksmiths"], [])

    @patch("apps.logs_engine.views.get_google_maps_client")
    @patch("apps.logs_engine.views.get_handl_client")
    def test_drive_time_over_two_hours_excluded_even_with_a_resolved_distance(self, mock_get_handl, mock_get_maps):
        # A postcode-only home skips the straight-line pre-filter (see
        # test_postcode_only_home_never_distance_filtered), so this real
        # drive-time cutoff is what actually keeps something 3+ hours
        # away off the list.
        Locksmith.objects.create(name="WGTK - Too Slow", home_postcode="EH1 1AA")
        mock_get_handl.return_value = MagicMock(
            get_job_details=MagicMock(return_value={"501179": _job_with_location()}),
            get_future_locksmith_attendances=MagicMock(return_value=[]),
        )
        mock_get_maps.return_value = MagicMock(get_distances=MagicMock(return_value=[
            LocksmithDistance(origin="EH1 1AA", distance_metres=560000.0, duration_seconds=7260, status="OK"),
        ]))

        response = self.client.get(reverse("logs_engine:lookup"), {"report_id": "501179"})

        self.assertEqual(response.context["nearest_locksmiths"], [])

    @patch("apps.logs_engine.views.get_google_maps_client")
    @patch("apps.logs_engine.views.get_handl_client")
    def test_drive_time_at_exactly_two_hours_included(self, mock_get_handl, mock_get_maps):
        Locksmith.objects.create(name="WGTK - Just Fine", home_postcode="EH1 1AA")
        mock_get_handl.return_value = MagicMock(
            get_job_details=MagicMock(return_value={"501179": _job_with_location()}),
            get_future_locksmith_attendances=MagicMock(return_value=[]),
        )
        mock_get_maps.return_value = MagicMock(get_distances=MagicMock(return_value=[
            LocksmithDistance(origin="EH1 1AA", distance_metres=190000.0, duration_seconds=7200, status="OK"),
        ]))

        response = self.client.get(reverse("logs_engine:lookup"), {"report_id": "501179"})

        self.assertEqual(len(response.context["nearest_locksmiths"]), 1)

    @patch("apps.logs_engine.views.get_google_maps_client")
    @patch("apps.logs_engine.views.get_handl_client")
    def test_future_job_beyond_drive_time_cap_excluded_too(self, mock_get_handl, mock_get_maps):
        # The cap applies regardless of which signal put a locksmith on
        # the list — an "already booked nearby" future job 3+ hours away
        # is exactly as unhelpful as a far-off home postcode.
        far_locksmith = Locksmith.objects.create(name="WGTK - Andrew S", home_postcode="")
        SoterLocksmithId.objects.create(locksmith=far_locksmith, soter_locksmith_id="1204")
        mock_get_handl.return_value = MagicMock(
            get_job_details=MagicMock(return_value={"501179": _job_with_location()}),
            get_future_locksmith_attendances=MagicMock(return_value=[
                FutureLocksmithAttendance(
                    report_id="502000",
                    soter_locksmith_id="1204",
                    locksmith_name="WGTK - Andrew S",
                    available_from=datetime.now() + timedelta(days=1),
                    vehicle_postcode="EH1 1AA",
                    vehicle_reg="AB20 CDE",
                ),
            ]),
        )
        mock_get_maps.return_value = MagicMock(get_distances=MagicMock(return_value=[
            LocksmithDistance(origin="EH1 1AA", distance_metres=560000.0, duration_seconds=7260, status="OK"),
        ]))

        response = self.client.get(reverse("logs_engine:lookup"), {"report_id": "501179"})

        self.assertEqual(response.context["nearest_locksmiths"], [])

    @patch("apps.logs_engine.views.get_google_maps_client")
    @patch("apps.logs_engine.views.get_handl_client")
    def test_locksmith_with_closer_future_job_beats_home_postcode(self, mock_get_handl, mock_get_maps):
        # This locksmith's home is far away, but they're already booked
        # to be right near this job soon — that should win.
        far_from_home = Locksmith.objects.create(name="WGTK - Andrew S", home_postcode="IP1 2AB")
        SoterLocksmithId.objects.create(locksmith=far_from_home, soter_locksmith_id="1204")

        mock_get_handl.return_value = MagicMock(
            get_job_details=MagicMock(return_value={"501179": _job_with_location()}),
            get_future_locksmith_attendances=MagicMock(return_value=[
                FutureLocksmithAttendance(
                    report_id="502000",
                    soter_locksmith_id="1204",
                    locksmith_name="WGTK - Andrew S",
                    available_from=datetime.now() + timedelta(days=1),
                    vehicle_postcode="NR14 8PL",
                    vehicle_reg="AB20 CDE",
                ),
            ]),
        )
        # Two calls: the outbound leg (home + future-job origins), then
        # a second for the return-trip leg back to their real home
        # (IP1 2AB) — a different route from either outbound origin,
        # so it needs its own lookup rather than reusing one of the
        # above.
        mock_get_maps.return_value = MagicMock(get_distances=MagicMock(side_effect=[
            [
                LocksmithDistance(origin="IP1 2AB", distance_metres=40000.0, duration_seconds=3000, status="OK"),
                LocksmithDistance(origin="NR14 8PL", distance_metres=8369.0, duration_seconds=720, status="OK"),
            ],
            [
                LocksmithDistance(origin="IP1 2AB", distance_metres=40000.0, duration_seconds=3000, status="OK"),
            ],
        ]))

        response = self.client.get(reverse("logs_engine:lookup"), {"report_id": "501179"})

        nearest = response.context["nearest_locksmiths"]
        self.assertEqual(len(nearest), 1)
        ranked = nearest[0]
        self.assertEqual(ranked.locksmith.pk, far_from_home.pk)
        self.assertEqual(ranked.distance.distance_metres, 8369.0)
        self.assertIsNotNone(ranked.attendance)
        self.assertEqual(ranked.attendance.vehicle_reg, "AB20 CDE")
        self.assertContains(response, "Already booked nearby")
        # 12 min there (NR14 8PL) + 40 min job + 50 min back home (IP1 2AB)
        self.assertEqual(ranked.total_minutes, 12 + 40 + 50)
        self.assertEqual(ranked.future_job_count, 1)

        first_call_origins = mock_get_maps.return_value.get_distances.call_args_list[0][0][0]
        self.assertEqual(first_call_origins, ["IP1 2AB", "NR14 8PL"])
        second_call_origins = mock_get_maps.return_value.get_distances.call_args_list[1][0][0]
        self.assertEqual(second_call_origins, ["IP1 2AB"])

    @patch("apps.logs_engine.views.get_google_maps_client")
    @patch("apps.logs_engine.views.get_handl_client")
    def test_future_job_count_reflects_every_booked_job_not_just_soonest(self, mock_get_handl, mock_get_maps):
        # total_minutes only ever factors in the SOONEST future job, but
        # future_job_count should reflect all of them — the whole point
        # is to flag a locksmith who looks free on paper but actually
        # has several jobs already booked in.
        locksmith = Locksmith.objects.create(name="WGTK - Busy Andrew", home_postcode="NR14 8PL")
        SoterLocksmithId.objects.create(locksmith=locksmith, soter_locksmith_id="1204")

        mock_get_handl.return_value = MagicMock(
            get_job_details=MagicMock(return_value={"501179": _job_with_location()}),
            get_future_locksmith_attendances=MagicMock(return_value=[
                FutureLocksmithAttendance(
                    report_id="502000", soter_locksmith_id="1204", locksmith_name="WGTK - Busy Andrew",
                    available_from=datetime.now() + timedelta(days=3),
                    vehicle_postcode="CB1 2AB", vehicle_reg="LATER1",
                ),
                FutureLocksmithAttendance(
                    report_id="502001", soter_locksmith_id="1204", locksmith_name="WGTK - Busy Andrew",
                    available_from=datetime.now() + timedelta(days=1),  # soonest
                    vehicle_postcode="IP1 2AB", vehicle_reg="SOONEST1",
                ),
                FutureLocksmithAttendance(
                    report_id="502002", soter_locksmith_id="1204", locksmith_name="WGTK - Busy Andrew",
                    available_from=datetime.now() + timedelta(days=5),
                    vehicle_postcode="PE1 3AA", vehicle_reg="LATER2",
                ),
            ]),
        )
        mock_get_maps.return_value = MagicMock(get_distances=MagicMock(side_effect=[
            [
                LocksmithDistance(origin="NR14 8PL", distance_metres=30000.0, duration_seconds=3600, status="OK"),
                LocksmithDistance(origin="IP1 2AB", distance_metres=8369.0, duration_seconds=720, status="OK"),
            ],
            [
                LocksmithDistance(origin="NR14 8PL", distance_metres=8369.0, duration_seconds=720, status="OK"),
            ],
        ]))

        response = self.client.get(reverse("logs_engine:lookup"), {"report_id": "501179"})

        nearest = response.context["nearest_locksmiths"]
        self.assertEqual(len(nearest), 1)
        ranked = nearest[0]
        self.assertEqual(ranked.future_job_count, 3)
        self.assertEqual(ranked.attendance.vehicle_reg, "SOONEST1")  # only the soonest wins the outbound leg

    @override_settings(GOOGLE_MAPS_API_KEY="test-key")
    @patch("apps.logs_engine.views.get_google_maps_client")
    @patch("apps.logs_engine.views.get_handl_client")
    def test_map_url_present_when_api_key_configured(self, mock_get_handl, mock_get_maps):
        locksmith = Locksmith.objects.create(name="WGTK - Nearby", home_postcode="NR14 8PL")
        SoterLocksmithId.objects.create(locksmith=locksmith, soter_locksmith_id="1204")
        mock_get_handl.return_value = MagicMock(
            get_job_details=MagicMock(return_value={"501179": _job_with_location()}),
            get_future_locksmith_attendances=MagicMock(return_value=[
                FutureLocksmithAttendance(
                    report_id="502000", soter_locksmith_id="1204", locksmith_name="WGTK - Nearby",
                    available_from=datetime.now() + timedelta(days=1),
                    vehicle_postcode="IP1 2AB", vehicle_reg="AB20 CDE",
                ),
            ]),
        )
        mock_get_maps.return_value = MagicMock(get_distances=MagicMock(return_value=[
            LocksmithDistance(origin="NR14 8PL", distance_metres=8369.0, duration_seconds=720, status="OK"),
            LocksmithDistance(origin="IP1 2AB", distance_metres=40000.0, duration_seconds=3000, status="OK"),
        ]))

        response = self.client.get(reverse("logs_engine:lookup"), {"report_id": "501179"})

        ranked = response.context["nearest_locksmiths"][0]
        self.assertTrue(ranked.map_url)
        self.assertIn("staticmap", ranked.map_url)
        self.assertIn("IP1+2AB", ranked.map_url)  # their booked job, in red
        self.assertContains(response, "map-preview-trigger")

    @patch("apps.logs_engine.views.get_google_maps_client")
    @patch("apps.logs_engine.views.get_handl_client")
    def test_map_url_empty_without_api_key(self, mock_get_handl, mock_get_maps):
        Locksmith.objects.create(name="WGTK - Nearby", home_postcode="NR14 8PL")
        mock_get_handl.return_value = MagicMock(
            get_job_details=MagicMock(return_value={"501179": _job_with_location()}),
            get_future_locksmith_attendances=MagicMock(return_value=[]),
        )
        mock_get_maps.return_value = MagicMock(get_distances=MagicMock(return_value=[
            LocksmithDistance(origin="NR14 8PL", distance_metres=8369.0, duration_seconds=720, status="OK"),
        ]))

        response = self.client.get(reverse("logs_engine:lookup"), {"report_id": "501179"})

        ranked = response.context["nearest_locksmiths"][0]
        self.assertEqual(ranked.map_url, "")
        self.assertEqual(ranked.future_job_count, 0)

    @patch("apps.logs_engine.views.get_google_maps_client")
    @patch("apps.logs_engine.views.get_handl_client")
    def test_home_postcode_wins_when_closer_than_future_job(self, mock_get_handl, mock_get_maps):
        locksmith = Locksmith.objects.create(name="WGTK - Andrew S", home_postcode="NR14 8PL")
        SoterLocksmithId.objects.create(locksmith=locksmith, soter_locksmith_id="1204")

        mock_get_handl.return_value = MagicMock(
            get_job_details=MagicMock(return_value={"501179": _job_with_location()}),
            get_future_locksmith_attendances=MagicMock(return_value=[
                FutureLocksmithAttendance(
                    report_id="502000",
                    soter_locksmith_id="1204",
                    locksmith_name="WGTK - Andrew S",
                    available_from=datetime.now() + timedelta(days=1),
                    vehicle_postcode="IP1 2AB",
                    vehicle_reg="AB20 CDE",
                ),
            ]),
        )
        mock_get_maps.return_value = MagicMock(get_distances=MagicMock(return_value=[
            LocksmithDistance(origin="NR14 8PL", distance_metres=8369.0, duration_seconds=720, status="OK"),
            LocksmithDistance(origin="IP1 2AB", distance_metres=40000.0, duration_seconds=3000, status="OK"),
        ]))

        response = self.client.get(reverse("logs_engine:lookup"), {"report_id": "501179"})

        nearest = response.context["nearest_locksmiths"]
        self.assertEqual(len(nearest), 1)
        ranked = nearest[0]
        self.assertEqual(ranked.distance.distance_metres, 8369.0)
        self.assertIsNone(ranked.attendance)
        self.assertContains(response, "Home location")
        # Home-based: no second API call needed for the return leg — it's
        # assumed symmetric to the outbound one and reused as-is.
        mock_get_maps.return_value.get_distances.assert_called_once()
        self.assertEqual(ranked.total_minutes, 12 + 40 + 12)

    @patch("apps.logs_engine.views.get_google_maps_client")
    @patch("apps.logs_engine.views.get_handl_client")
    def test_future_attendance_for_unknown_locksmith_ignored(self, mock_get_handl, mock_get_maps):
        Locksmith.objects.create(name="WGTK - Nearby", home_postcode="NR14 8PL")
        mock_get_handl.return_value = MagicMock(
            get_job_details=MagicMock(return_value={"501179": _job_with_location()}),
            get_future_locksmith_attendances=MagicMock(return_value=[
                FutureLocksmithAttendance(
                    report_id="502000",
                    soter_locksmith_id="9999",  # no matching Locksmith
                    locksmith_name="WGTK - Someone Else",
                    available_from=datetime.now() + timedelta(days=1),
                    vehicle_postcode="IP1 2AB",
                    vehicle_reg="AB20 CDE",
                ),
            ]),
        )
        mock_get_maps.return_value = MagicMock(get_distances=MagicMock(return_value=[
            LocksmithDistance(origin="NR14 8PL", distance_metres=8369.0, duration_seconds=720, status="OK"),
        ]))

        response = self.client.get(reverse("logs_engine:lookup"), {"report_id": "501179"})

        # Only the home-postcode origin should have been queried — the
        # unmatched attendance never turns into a second origin.
        call_origins = mock_get_maps.return_value.get_distances.call_args[0][0]
        self.assertEqual(call_origins, ["NR14 8PL"])

    @patch("apps.logs_engine.views.get_google_maps_client")
    @patch("apps.logs_engine.views.get_handl_client")
    def test_future_attendance_lookup_failure_falls_back_to_home_postcode(self, mock_get_handl, mock_get_maps):
        Locksmith.objects.create(name="WGTK - Nearby", home_postcode="NR14 8PL")
        mock_get_handl.return_value = MagicMock(
            get_job_details=MagicMock(return_value={"501179": _job_with_location()}),
            get_future_locksmith_attendances=MagicMock(side_effect=Exception("boom")),
        )
        mock_get_maps.return_value = MagicMock(get_distances=MagicMock(return_value=[
            LocksmithDistance(origin="NR14 8PL", distance_metres=8369.0, duration_seconds=720, status="OK"),
        ]))

        response = self.client.get(reverse("logs_engine:lookup"), {"report_id": "501179"})

        self.assertEqual(response.status_code, 200)
        nearest = response.context["nearest_locksmiths"]
        self.assertEqual(len(nearest), 1)
        self.assertIsNone(nearest[0].attendance)

    @patch("apps.logs_engine.views.get_google_maps_client")
    @patch("apps.logs_engine.views.get_handl_client")
    def test_sorted_by_total_minutes_not_just_outbound_distance(self, mock_get_handl, mock_get_maps):
        # Locksmith A has a much shorter drive there (via an
        # already-booked future job right next to this one) but a very
        # long trip back to their real home afterwards; Locksmith B has
        # a longer drive there but gets home again quickly. Once the
        # return trip and job time are both counted, B is the better
        # overall pick, even though A looks unbeatable on outbound
        # distance alone.
        locksmith_a = Locksmith.objects.create(name="WGTK - Short There Long Back", home_postcode="FAR AWAY")
        SoterLocksmithId.objects.create(locksmith=locksmith_a, soter_locksmith_id="1204")
        locksmith_b = Locksmith.objects.create(name="WGTK - Steady Both Ways", home_postcode="NR14 8PL")

        mock_get_handl.return_value = MagicMock(
            get_job_details=MagicMock(return_value={"501179": _job_with_location()}),
            get_future_locksmith_attendances=MagicMock(return_value=[
                FutureLocksmithAttendance(
                    report_id="502000",
                    soter_locksmith_id="1204",
                    locksmith_name="WGTK - Short There Long Back",
                    available_from=datetime.now() + timedelta(days=1),
                    vehicle_postcode="CLOSE BY",
                    vehicle_reg="AB20 CDE",
                ),
            ]),
        )
        mock_get_maps.return_value = MagicMock(get_distances=MagicMock(side_effect=[
            # Outbound leg, in origin order: A's home (irrelevant, A's
            # future job wins on distance), A's future job (very
            # close), B's home.
            [
                LocksmithDistance(origin="FAR AWAY", distance_metres=100000.0, duration_seconds=6000, status="OK"),
                LocksmithDistance(origin="CLOSE BY", distance_metres=1000.0, duration_seconds=600, status="OK"),
                LocksmithDistance(origin="NR14 8PL", distance_metres=30000.0, duration_seconds=3600, status="OK"),
            ],
            # Return leg, only needed for A (future-job-based) — B's is
            # reused/symmetric from its outbound leg above.
            [
                LocksmithDistance(origin="FAR AWAY", distance_metres=600000.0, duration_seconds=12000, status="OK"),
            ],
        ]))

        response = self.client.get(reverse("logs_engine:lookup"), {"report_id": "501179"})

        nearest = response.context["nearest_locksmiths"]
        self.assertEqual([r.locksmith.pk for r in nearest], [locksmith_b.pk, locksmith_a.pk])
        by_pk = {r.locksmith.pk: r for r in nearest}
        self.assertEqual(by_pk[locksmith_a.pk].total_minutes, 10 + 40 + 200)
        self.assertEqual(by_pk[locksmith_b.pk].total_minutes, 60 + 40 + 60)


class DriveTimeClassFilterTests(TestCase):
    def test_boundaries(self):
        self.assertEqual(drive_time_class(0), "drive-time-green")
        self.assertEqual(drive_time_class(45), "drive-time-green")
        self.assertEqual(drive_time_class(46), "drive-time-amber")
        self.assertEqual(drive_time_class(60), "drive-time-amber")
        self.assertEqual(drive_time_class(61), "drive-time-pale-red")
        self.assertEqual(drive_time_class(90), "drive-time-pale-red")
        self.assertEqual(drive_time_class(91), "drive-time-red")
        self.assertEqual(drive_time_class(200), "drive-time-red")

    def test_none_is_blank(self):
        self.assertEqual(drive_time_class(None), "")


class StraightLineMilesTests(TestCase):
    def test_known_distance_london_to_paris(self):
        # Real-world reference distance, ~213 miles as the crow flies.
        miles = _straight_line_miles(51.5074, -0.1278, 48.8566, 2.3522)
        self.assertAlmostEqual(miles, 213, delta=5)

    def test_zero_for_the_same_point(self):
        self.assertAlmostEqual(_straight_line_miles(52.63, 1.29, 52.63, 1.29), 0, delta=0.001)
