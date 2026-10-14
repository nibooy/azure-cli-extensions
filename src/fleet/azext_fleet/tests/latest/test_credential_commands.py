# --------------------------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License.txt in the project root for license information.
# --------------------------------------------------------------------------------------------

import unittest
from unittest.mock import Mock

from azure.cli.core.azclierror import InvalidArgumentValueError, MutuallyExclusiveArgumentError

from azext_fleet.custom import (
    _build_member_credential_request,
    list_member_credentials,
    list_namespace_credentials,
    list_namespace_member_credentials,
)
from azext_fleet.vendored_sdks.v2026_11_02_preview.models import FleetMemberCredentialRequest


class _FakeCmd:
    def get_models(self, name, **_kwargs):
        if name != "FleetMemberCredentialRequest":
            raise AssertionError(f"unexpected model requested: {name}")
        return FleetMemberCredentialRequest


class CredentialCommandsTestCase(unittest.TestCase):
    def setUp(self):
        self.cmd = _FakeCmd()
        self.client = Mock()

    def test_builds_request_for_selected_members(self):
        request = _build_member_credential_request(self.cmd, ["member-1", "member-2"])

        self.assertEqual(request.member_names, ["member-1", "member-2"])
        self.assertIsNone(request.continuation_token)
        self.assertEqual(request.serialize(), {
            "memberNames": ["member-1", "member-2"],
        })

    def test_builds_request_for_continuation_token(self):
        request = _build_member_credential_request(
            self.cmd,
            continuation_token="next-page"
        )

        self.assertIsNone(request.member_names)
        self.assertEqual(request.continuation_token, "next-page")
        self.assertEqual(request.serialize(), {
            "continuationToken": "next-page",
        })

    def test_builds_empty_request_for_all_members(self):
        request = _build_member_credential_request(self.cmd)

        self.assertEqual(request.serialize(), {})

    def test_rejects_members_with_continuation_token(self):
        with self.assertRaises(MutuallyExclusiveArgumentError):
            _build_member_credential_request(
                self.cmd,
                ["member-1"],
                "next-page"
            )

    def test_rejects_more_than_100_members(self):
        with self.assertRaises(InvalidArgumentValueError):
            _build_member_credential_request(
                self.cmd,
                [f"member-{index}" for index in range(101)]
            )

    def test_rejects_duplicate_members(self):
        with self.assertRaises(InvalidArgumentValueError):
            _build_member_credential_request(
                self.cmd,
                ["member-1", "member-1"]
            )

    def test_lists_fleet_member_credentials(self):
        list_member_credentials(
            self.cmd,
            self.client,
            "resource-group",
            "fleet",
            member_names=["member-1"]
        )

        request = self.client.list_member_credentials.call_args.kwargs["body"]
        self.assertEqual(request.member_names, ["member-1"])
        self.client.list_member_credentials.assert_called_once_with(
            resource_group_name="resource-group",
            fleet_name="fleet",
            body=request
        )

    def test_lists_namespace_credentials(self):
        list_namespace_credentials(
            self.cmd,
            self.client,
            "resource-group",
            "fleet",
            "namespace"
        )

        self.client.list_credentials.assert_called_once_with(
            resource_group_name="resource-group",
            fleet_name="fleet",
            managed_namespace_name="namespace"
        )

    def test_lists_namespace_member_credentials(self):
        list_namespace_member_credentials(
            self.cmd,
            self.client,
            "resource-group",
            "fleet",
            "namespace",
            continuation_token="next-page"
        )

        request = self.client.list_member_credentials.call_args.kwargs["body"]
        self.assertEqual(request.continuation_token, "next-page")
        self.client.list_member_credentials.assert_called_once_with(
            resource_group_name="resource-group",
            fleet_name="fleet",
            managed_namespace_name="namespace",
            body=request
        )


if __name__ == "__main__":
    unittest.main()
