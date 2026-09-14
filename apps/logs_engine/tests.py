from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.integrations.google_maps import LocksmithDistance
from apps.integrations.handl import FutureLocksmithAttendance, JobDetails
from apps.integrations.teams_shifts import ShiftAssignment
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
        self.assertEqual([c.locksmith.pk for c in nearest], [near.pk, far.pk])
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
        card = nearest[0]
        self.assertEqual(card.locksmith.pk, no_home_postcode.pk)
        self.assertEqual(len(card.options), 1)
        option = card.options[0]
        self.assertIsNotNone(option.attendance)
        # No home location on file at all means no return-trip leg can
        # be resolved either — total_minutes degrades to None rather
        # than guessing, and get_distances is only ever called once
        # (for the outbound leg) since there's nowhere to look up a
        # return trip to.
        self.assertIsNone(option.total_minutes)
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
        card = nearest[0]
        self.assertEqual(card.locksmith.pk, far_from_home.pk)
        # Both options survive (home postcode + the closer future job),
        # best (lowest total_minutes) first.
        self.assertEqual(len(card.options), 2)
        best = card.options[0]
        self.assertEqual(best.distance.distance_metres, 8369.0)
        self.assertIsNotNone(best.attendance)
        self.assertEqual(best.attendance.vehicle_reg, "AB20 CDE")
        self.assertContains(response, "Already booked nearby")
        # 12 min there (NR14 8PL) + 40 min job + 50 min back home (IP1 2AB)
        self.assertEqual(best.total_minutes, 12 + 40 + 50)
        # The booked job is tomorrow, not today, so today's count is 0
        # even though it's still an option above.
        self.assertEqual(card.future_job_count, 0)

        first_call_origins = mock_get_maps.return_value.get_distances.call_args_list[0][0][0]
        self.assertEqual(first_call_origins, ["IP1 2AB", "NR14 8PL"])
        second_call_origins = mock_get_maps.return_value.get_distances.call_args_list[1][0][0]
        self.assertEqual(second_call_origins, ["IP1 2AB"])

    @patch("apps.logs_engine.views.get_google_maps_client")
    @patch("apps.logs_engine.views.get_handl_client")
    def test_card_lists_every_booked_job_within_window_as_its_own_option(self, mock_get_handl, mock_get_maps):
        # Every job booked within the window becomes its own option on
        # the card (not just one "best" row), even though none of them
        # are booked for today — future_job_count is scoped to today
        # only (see test_todays_job_count_excludes_jobs_on_other_days),
        # a different, narrower slice than the options shown here.
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
        # All three are within the 7-day window, so all become
        # candidate origins alongside home — LATER1 (booked for day 3,
        # not the soonest) is deliberately the closest one, to prove the
        # card sorts it first even though it isn't chronologically next.
        mock_get_maps.return_value = MagicMock(get_distances=MagicMock(side_effect=[
            [
                LocksmithDistance(origin="NR14 8PL", distance_metres=30000.0, duration_seconds=3600, status="OK"),
                LocksmithDistance(origin="CB1 2AB", distance_metres=5000.0, duration_seconds=600, status="OK"),
                LocksmithDistance(origin="IP1 2AB", distance_metres=8369.0, duration_seconds=720, status="OK"),
                LocksmithDistance(origin="PE1 3AA", distance_metres=20000.0, duration_seconds=1800, status="OK"),
            ],
            [
                LocksmithDistance(origin="NR14 8PL", distance_metres=8369.0, duration_seconds=720, status="OK"),
            ],
        ]))

        response = self.client.get(reverse("logs_engine:lookup"), {"report_id": "501179"})

        nearest = response.context["nearest_locksmiths"]
        self.assertEqual(len(nearest), 1)
        card = nearest[0]
        # None of the 3 booked jobs are today, so today's count is 0
        # even though all 3 are close enough in the week ahead to
        # appear as options below.
        self.assertEqual(card.future_job_count, 0)
        # All 4 candidates (home + 3 future jobs) survive as options.
        self.assertEqual(len(card.options), 4)
        self.assertEqual(card.options[0].attendance.vehicle_reg, "LATER1")  # closest wins, not the soonest
        vehicle_regs = {o.attendance.vehicle_reg for o in card.options if o.attendance}
        self.assertEqual(vehicle_regs, {"LATER1", "SOONEST1", "LATER2"})

        first_call_origins = mock_get_maps.return_value.get_distances.call_args_list[0][0][0]
        self.assertEqual(first_call_origins, ["NR14 8PL", "CB1 2AB", "IP1 2AB", "PE1 3AA"])

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
                    available_from=datetime.now() + timedelta(hours=2),  # today
                    vehicle_postcode="IP1 2AB", vehicle_reg="AB20 CDE",
                ),
            ]),
        )
        mock_get_maps.return_value = MagicMock(get_distances=MagicMock(return_value=[
            LocksmithDistance(origin="NR14 8PL", distance_metres=8369.0, duration_seconds=720, status="OK"),
            LocksmithDistance(origin="IP1 2AB", distance_metres=40000.0, duration_seconds=3000, status="OK"),
        ]))

        response = self.client.get(reverse("logs_engine:lookup"), {"report_id": "501179"})

        card = response.context["nearest_locksmiths"][0]
        self.assertTrue(card.map_url)
        self.assertIn("staticmap", card.map_url)
        self.assertIn("IP1+2AB", card.map_url)  # their booked job today, in red
        self.assertContains(response, "map-preview-trigger")

    @override_settings(GOOGLE_MAPS_API_KEY="test-key")
    @patch("apps.logs_engine.views.get_google_maps_client")
    @patch("apps.logs_engine.views.get_handl_client")
    def test_todays_job_count_excludes_jobs_on_other_days(self, mock_get_handl, mock_get_maps):
        # future_job_count/map_url are meant to answer "how busy is this
        # locksmith TODAY", not "ever" — a job booked in for later this
        # week shouldn't count or show up on the map here, even though
        # it's still a selectable option in its own right (see
        # test_card_lists_every_booked_job_within_window_as_its_own_option).
        locksmith = Locksmith.objects.create(name="WGTK - Nearby", home_postcode="NR14 8PL")
        SoterLocksmithId.objects.create(locksmith=locksmith, soter_locksmith_id="1204")
        mock_get_handl.return_value = MagicMock(
            get_job_details=MagicMock(return_value={"501179": _job_with_location()}),
            get_future_locksmith_attendances=MagicMock(return_value=[
                FutureLocksmithAttendance(
                    report_id="502000", soter_locksmith_id="1204", locksmith_name="WGTK - Nearby",
                    available_from=datetime.now() + timedelta(hours=2),  # today
                    vehicle_postcode="IP1 2AB", vehicle_reg="TODAY1",
                ),
                FutureLocksmithAttendance(
                    report_id="502001", soter_locksmith_id="1204", locksmith_name="WGTK - Nearby",
                    available_from=datetime.now() + timedelta(days=3),
                    vehicle_postcode="CB1 2AB", vehicle_reg="LATER1",
                ),
            ]),
        )
        mock_get_maps.return_value = MagicMock(get_distances=MagicMock(return_value=[
            LocksmithDistance(origin="NR14 8PL", distance_metres=8369.0, duration_seconds=720, status="OK"),
            LocksmithDistance(origin="IP1 2AB", distance_metres=8369.0, duration_seconds=720, status="OK"),
            LocksmithDistance(origin="CB1 2AB", distance_metres=5000.0, duration_seconds=600, status="OK"),
        ]))

        response = self.client.get(reverse("logs_engine:lookup"), {"report_id": "501179"})

        card = response.context["nearest_locksmiths"][0]
        # Only the one booked in for today counts.
        self.assertEqual(card.future_job_count, 1)
        self.assertIn("IP1+2AB", card.map_url)  # today's job, plotted
        self.assertNotIn("CB1+2AB", card.map_url)  # later this week, not plotted
        # Both still show up as their own selectable options though.
        vehicle_regs = {o.attendance.vehicle_reg for o in card.options if o.attendance}
        self.assertEqual(vehicle_regs, {"TODAY1", "LATER1"})

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

        card = response.context["nearest_locksmiths"][0]
        self.assertEqual(card.map_url, "")
        self.assertEqual(card.future_job_count, 0)

    @patch("apps.logs_engine.views.get_teams_shifts_client")
    @patch("apps.logs_engine.views.get_google_maps_client")
    @patch("apps.logs_engine.views.get_handl_client")
    def test_on_shift_true_when_teams_reports_them_on_shift_right_now(
        self, mock_get_handl, mock_get_maps, mock_get_shifts
    ):
        from django.utils import timezone as django_timezone

        Locksmith.objects.create(name="WGTK - Nearby", home_postcode="NR14 8PL", email="andrew.s@wgtk.co.uk")
        mock_get_handl.return_value = MagicMock(
            get_job_details=MagicMock(return_value={"501179": _job_with_location()}),
            get_future_locksmith_attendances=MagicMock(return_value=[]),
        )
        mock_get_maps.return_value = MagicMock(get_distances=MagicMock(return_value=[
            LocksmithDistance(origin="NR14 8PL", distance_metres=8369.0, duration_seconds=720, status="OK"),
        ]))
        now = django_timezone.localtime(django_timezone.now()).replace(tzinfo=None)
        mock_get_shifts.return_value = MagicMock(list_shifts_for_date=MagicMock(return_value=[
            ShiftAssignment(
                email="andrew.s@wgtk.co.uk",
                shift_start=now - timedelta(hours=1), shift_end=now + timedelta(hours=1),
            ),
        ]))

        response = self.client.get(reverse("logs_engine:lookup"), {"report_id": "501179"})

        card = response.context["nearest_locksmiths"][0]
        self.assertTrue(card.on_shift)
        self.assertContains(response, "On shift")
        mock_get_shifts.return_value.list_shifts_for_date.assert_called_once_with(now.date())

    @patch("apps.logs_engine.views.get_teams_shifts_client")
    @patch("apps.logs_engine.views.get_google_maps_client")
    @patch("apps.logs_engine.views.get_handl_client")
    def test_on_shift_matches_by_real_login_email_not_handl_synced_one(
        self, mock_get_handl, mock_get_maps, mock_get_shifts
    ):
        # Confirmed live: Handl/Soter's own email for a locksmith can be
        # on a completely different domain (e.g. "...@soterps.com")
        # from the address they actually sign into Microsoft/Teams
        # with ("...@wgtk.co.uk") — once they've logged into the
        # portal at least once, Locksmith.user.email holds that real,
        # Microsoft-verified address, so that's what should be used
        # here, not the Handl-synced Locksmith.email.
        from django.utils import timezone as django_timezone

        user = get_user_model().objects.create_user(
            username="michael.mccrossan", email="michael.mccrossan@wgtk.co.uk", password="x",
        )
        locksmith = Locksmith.objects.create(
            name="WGTK - Michael M", home_postcode="NR14 8PL",
            email="michaelmccrossan@soterps.com", user=user,
        )
        mock_get_handl.return_value = MagicMock(
            get_job_details=MagicMock(return_value={"501179": _job_with_location()}),
            get_future_locksmith_attendances=MagicMock(return_value=[]),
        )
        mock_get_maps.return_value = MagicMock(get_distances=MagicMock(return_value=[
            LocksmithDistance(origin="NR14 8PL", distance_metres=8369.0, duration_seconds=720, status="OK"),
        ]))
        now = django_timezone.localtime(django_timezone.now()).replace(tzinfo=None)
        mock_get_shifts.return_value = MagicMock(list_shifts_for_date=MagicMock(return_value=[
            ShiftAssignment(
                email="michael.mccrossan@wgtk.co.uk",
                shift_start=now - timedelta(hours=1), shift_end=now + timedelta(hours=1),
            ),
        ]))

        response = self.client.get(reverse("logs_engine:lookup"), {"report_id": "501179"})

        card = response.context["nearest_locksmiths"][0]
        self.assertEqual(card.locksmith.pk, locksmith.pk)
        self.assertTrue(card.on_shift)

    @patch("apps.logs_engine.views.get_teams_shifts_client")
    @patch("apps.logs_engine.views.get_google_maps_client")
    @patch("apps.logs_engine.views.get_handl_client")
    def test_on_shift_false_when_scheduled_but_outside_current_shift_window(
        self, mock_get_handl, mock_get_maps, mock_get_shifts
    ):
        from django.utils import timezone as django_timezone

        Locksmith.objects.create(name="WGTK - Nearby", home_postcode="NR14 8PL", email="andrew.s@wgtk.co.uk")
        mock_get_handl.return_value = MagicMock(
            get_job_details=MagicMock(return_value={"501179": _job_with_location()}),
            get_future_locksmith_attendances=MagicMock(return_value=[]),
        )
        mock_get_maps.return_value = MagicMock(get_distances=MagicMock(return_value=[
            LocksmithDistance(origin="NR14 8PL", distance_metres=8369.0, duration_seconds=720, status="OK"),
        ]))
        now = django_timezone.localtime(django_timezone.now()).replace(tzinfo=None)
        mock_get_shifts.return_value = MagicMock(list_shifts_for_date=MagicMock(return_value=[
            # Rostered on today, but that shift already finished hours ago.
            ShiftAssignment(
                email="andrew.s@wgtk.co.uk",
                shift_start=now - timedelta(hours=10), shift_end=now - timedelta(hours=5),
            ),
        ]))

        response = self.client.get(reverse("logs_engine:lookup"), {"report_id": "501179"})

        card = response.context["nearest_locksmiths"][0]
        self.assertIs(card.on_shift, False)
        self.assertContains(response, "Off shift")

    @patch("apps.logs_engine.views.get_teams_shifts_client")
    @patch("apps.logs_engine.views.get_google_maps_client")
    @patch("apps.logs_engine.views.get_handl_client")
    def test_on_shift_none_when_locksmith_has_no_email(self, mock_get_handl, mock_get_maps, mock_get_shifts):
        Locksmith.objects.create(name="WGTK - Nearby", home_postcode="NR14 8PL")  # no email
        mock_get_handl.return_value = MagicMock(
            get_job_details=MagicMock(return_value={"501179": _job_with_location()}),
            get_future_locksmith_attendances=MagicMock(return_value=[]),
        )
        mock_get_maps.return_value = MagicMock(get_distances=MagicMock(return_value=[
            LocksmithDistance(origin="NR14 8PL", distance_metres=8369.0, duration_seconds=720, status="OK"),
        ]))

        response = self.client.get(reverse("logs_engine:lookup"), {"report_id": "501179"})

        card = response.context["nearest_locksmiths"][0]
        self.assertIsNone(card.on_shift)
        mock_get_shifts.assert_not_called()

    @patch("apps.logs_engine.views.get_teams_shifts_client")
    @patch("apps.logs_engine.views.get_google_maps_client")
    @patch("apps.logs_engine.views.get_handl_client")
    def test_on_shift_none_when_teams_lookup_fails(self, mock_get_handl, mock_get_maps, mock_get_shifts):
        Locksmith.objects.create(name="WGTK - Nearby", home_postcode="NR14 8PL", email="andrew.s@wgtk.co.uk")
        mock_get_handl.return_value = MagicMock(
            get_job_details=MagicMock(return_value={"501179": _job_with_location()}),
            get_future_locksmith_attendances=MagicMock(return_value=[]),
        )
        mock_get_maps.return_value = MagicMock(get_distances=MagicMock(return_value=[
            LocksmithDistance(origin="NR14 8PL", distance_metres=8369.0, duration_seconds=720, status="OK"),
        ]))
        mock_get_shifts.return_value = MagicMock(
            list_shifts_for_date=MagicMock(side_effect=Exception(
                "Graph request failed: 403 Forbidden — Insufficient privileges"
            ))
        )

        response = self.client.get(reverse("logs_engine:lookup"), {"report_id": "501179"})

        self.assertEqual(response.status_code, 200)
        card = response.context["nearest_locksmiths"][0]
        self.assertIsNone(card.on_shift)
        self.assertEqual(
            response.context["on_shift_error"],
            "Graph request failed: 403 Forbidden — Insufficient privileges",
        )
        self.assertContains(response, "Shift status (Teams) couldn't be checked")
        self.assertContains(response, "Insufficient privileges")

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
        # Two calls: the outbound leg (home + future-job origins), then
        # a second for the return-trip leg back home, needed because the
        # future-job option (IP1 2AB) also survives as an option in its
        # own right, not just the home one.
        mock_get_maps.return_value = MagicMock(get_distances=MagicMock(side_effect=[
            [
                LocksmithDistance(origin="NR14 8PL", distance_metres=8369.0, duration_seconds=720, status="OK"),
                LocksmithDistance(origin="IP1 2AB", distance_metres=40000.0, duration_seconds=3000, status="OK"),
            ],
            [
                LocksmithDistance(origin="NR14 8PL", distance_metres=8369.0, duration_seconds=720, status="OK"),
            ],
        ]))

        response = self.client.get(reverse("logs_engine:lookup"), {"report_id": "501179"})

        nearest = response.context["nearest_locksmiths"]
        self.assertEqual(len(nearest), 1)
        card = nearest[0]
        # Both options survive; home is the better one and sorts first.
        self.assertEqual(len(card.options), 2)
        best = card.options[0]
        self.assertEqual(best.distance.distance_metres, 8369.0)
        self.assertIsNone(best.attendance)
        self.assertContains(response, "Home location")
        self.assertEqual(best.total_minutes, 12 + 40 + 12)
        worst = card.options[1]
        self.assertIsNotNone(worst.attendance)
        self.assertEqual(worst.total_minutes, 50 + 40 + 12)

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
        self.assertIsNone(nearest[0].options[0].attendance)

    @patch("apps.logs_engine.views.get_google_maps_client")
    @patch("apps.logs_engine.views.get_handl_client")
    def test_sorted_by_total_minutes_not_just_outbound_distance(self, mock_get_handl, mock_get_maps):
        # Locksmith A has an option with a much shorter drive there (via
        # an already-booked future job right next to this one) but a
        # very long trip back to their real home afterwards, alongside
        # their home option (longer drive there, shorter drive back);
        # Locksmith B just has their home option, with a longer drive
        # there but a quick trip home. Once the return trip and job time
        # are both counted: within A's own card, the home option is
        # actually the better one (its options are sorted by total
        # time, not raw outbound distance) — and B still beats A
        # overall, since even A's best option comes in slower.
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
        self.assertEqual([c.locksmith.pk for c in nearest], [locksmith_b.pk, locksmith_a.pk])
        by_pk = {c.locksmith.pk: c for c in nearest}

        card_a = by_pk[locksmith_a.pk]
        self.assertEqual(len(card_a.options), 2)
        # Home (100 there + 40 job + 100 back = 240) actually beats the
        # future job (10 there + 40 job + 200 back = 250) once the whole
        # round trip is counted, even though the future job looked
        # unbeatable on outbound distance alone.
        self.assertIsNone(card_a.options[0].attendance)
        self.assertEqual(card_a.options[0].total_minutes, 100 + 40 + 100)
        self.assertIsNotNone(card_a.options[1].attendance)
        self.assertEqual(card_a.options[1].total_minutes, 10 + 40 + 200)

        card_b = by_pk[locksmith_b.pk]
        self.assertEqual(len(card_b.options), 1)
        self.assertEqual(card_b.options[0].total_minutes, 60 + 40 + 60)


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
