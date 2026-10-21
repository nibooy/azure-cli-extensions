# --------------------------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License.txt in the project root for license information.
# --------------------------------------------------------------------------------------------

import base64
import contextlib
import io
import os
import tempfile
import unittest
from unittest.mock import Mock, patch

import yaml
from knack.util import CLIError

from azure.cli.core.azclierror import InvalidArgumentValueError, MutuallyExclusiveArgumentError

from azext_fleet.custom import (
    _build_member_credential_request,
    list_member_credentials,
    list_namespace_credentials,
    list_namespace_member_credentials,
)
from azext_fleet.vendored_sdks.v2026_11_02_preview.models import (
    FleetManagedNamespaceCredentialResult,
    FleetManagedNamespaceCredentialResults,
    FleetMemberCredentialRequest,
    FleetMemberCredentialResults,
    FleetMemberManagedNamespaceCredentialResults,
)


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

class NamespaceCredentialsTestCase(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.config = {
            "apiVersion": "v1",
            "kind": "Config",
            "current-context": "hub",
            "clusters": [{"name": "hub", "cluster": {"server": "https://hub.example.com"}}],
            "contexts": [{"name": "hub", "context": {
                "cluster": "hub", "user": "hub-user", "namespace": "namespace",
            }}],
            "users": [{"name": "hub-user", "user": {"exec": {
                "apiVersion": "client.authentication.k8s.io/v1beta1",
                "command": "kubelogin",
                "args": ["get-token", "--login", "devicecode"],
            }}}],
        }
        credential = FleetManagedNamespaceCredentialResult()
        credential.value = base64.b64encode(yaml.safe_dump(self.config).encode("utf-8")).decode("ascii")
        response = FleetManagedNamespaceCredentialResults()
        response.kubeconfigs = [credential]
        self.client.list_credentials.return_value = response
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = os.path.join(directory.name, "config")

    def _get_credentials(self, **kwargs):
        return list_namespace_credentials(
            _FakeCmd(), self.client, "resource-group", "fleet", "namespace", **kwargs
        )

    @patch("azext_fleet.custom._convert_kubeconfig_to_azurecli")
    @patch("azext_fleet.custom.print_or_merge_credentials")
    def test_saves_to_default_path_then_converts(self, merge, convert):
        operations = Mock()
        operations.attach_mock(merge, "merge")
        operations.attach_mock(convert, "convert")

        self.assertIsNone(self._get_credentials())

        self.client.list_credentials.assert_called_once_with(
            resource_group_name="resource-group",
            fleet_name="fleet",
            managed_namespace_name="namespace"
        )
        default_path = os.path.join(os.path.expanduser("~"), ".kube", "config")
        merge.assert_called_once_with(
            default_path, yaml.safe_dump(self.config), False, None
        )
        convert.assert_called_once_with(default_path)
        self.assertEqual([call[0] for call in operations.mock_calls], ["merge", "convert"])

    @patch("azext_fleet.custom._convert_kubeconfig_to_azurecli")
    def test_merges_existing_config_and_sets_custom_context(self, convert):
        existing = {
            "apiVersion": "v1", "kind": "Config", "current-context": "existing",
            "clusters": [{"name": "existing", "cluster": {"server": "https://existing.example.com"}}],
            "contexts": [{"name": "existing", "context": {"cluster": "existing", "user": "existing"}}],
            "users": [{"name": "existing", "user": {"token": "test-token"}}],
        }
        with open(self.path, "w", encoding="utf-8") as stream:
            yaml.safe_dump(existing, stream)
        os.chmod(self.path, 0o600)

        self.assertIsNone(self._get_credentials(path=self.path, context_name="custom"))

        with open(self.path, encoding="utf-8") as stream:
            config = yaml.safe_load(stream)
        self.assertEqual(config["current-context"], "custom")
        self.assertIn(existing["contexts"][0], config["contexts"])
        self.assertIn(existing["clusters"][0], config["clusters"])
        self.assertIn(existing["users"][0], config["users"])
        context = next(item for item in config["contexts"] if item["name"] == "custom")
        self.assertEqual(context["context"]["namespace"], "namespace")
        convert.assert_called_once_with(self.path)

    @patch("azext_fleet.custom._convert_kubeconfig_to_azurecli")
    def test_overwrites_existing_credentials_without_prompting(self, _convert):
        self._get_credentials(path=self.path)
        self.config["clusters"][0]["cluster"]["server"] = "https://updated.example.com"
        self.client.list_credentials.return_value.kubeconfigs[0].value = (
            base64.b64encode(yaml.safe_dump(self.config).encode("utf-8")).decode("ascii")
        )

        with patch("azext_fleet._helpers.prompt_y_n") as prompt:
            self._get_credentials(path=self.path, overwrite_existing=True)
        prompt.assert_not_called()
        with open(self.path, encoding="utf-8") as stream:
            config = yaml.safe_load(stream)
        self.assertEqual(config["clusters"], self.config["clusters"])

    @patch("azext_fleet.custom.shutil.which", return_value="kubelogin")
    @patch("azext_fleet.custom.subprocess.run")
    def test_converts_saved_config_to_azurecli(self, run, _which):
        def convert(*args, **_kwargs):
            self.assertEqual(args[0], [
                "kubelogin", "convert-kubeconfig", "-l", "azurecli",
                "--kubeconfig", self.path,
            ])
            with open(self.path, encoding="utf-8") as stream:
                config = yaml.safe_load(stream)
            config["users"][0]["user"]["exec"]["args"][-1] = "azurecli"
            with open(self.path, "w", encoding="utf-8") as stream:
                yaml.safe_dump(config, stream)

        run.side_effect = convert
        self._get_credentials(path=self.path)
        run.assert_called_once()
        with open(self.path, encoding="utf-8") as stream:
            config = yaml.safe_load(stream)
        self.assertEqual(config["users"][0]["user"]["exec"]["args"][-1], "azurecli")
        self.assertEqual(config["contexts"][0]["context"]["namespace"], "namespace")
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    @patch("azext_fleet.custom.subprocess.run")
    def test_stdout_prints_yaml_without_json_or_conversion(self, run):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = self._get_credentials(path="-")
        self.assertIsNone(result)
        self.assertEqual(yaml.safe_load(output.getvalue()), self.config)
        run.assert_not_called()

    @patch("azext_fleet.custom._convert_kubeconfig_to_azurecli")
    def test_rejects_missing_credentials(self, convert):
        for response in (None, Mock(kubeconfigs=None), Mock(kubeconfigs=[]),
                         Mock(kubeconfigs=[Mock(value=None)])):
            with self.subTest(response=response):
                self.client.list_credentials.return_value = response
                with self.assertRaisesRegex(CLIError, "No Kubernetes credentials found"):
                    self._get_credentials(path=self.path)
        self.assertFalse(os.path.exists(self.path))
        convert.assert_not_called()

    def test_rejects_invalid_utf8(self):
        self.client.list_credentials.return_value.kubeconfigs[0].value = "/w=="
        with self.assertRaisesRegex(CLIError, "decode"):
            self._get_credentials(path=self.path)
        self.assertFalse(os.path.exists(self.path))

    def test_rejects_invalid_base64(self):
        self.client.list_credentials.return_value.kubeconfigs[0].value = "not base64!"
        with self.assertRaisesRegex(CLIError, "decode"):
            self._get_credentials(path=self.path)
        self.assertFalse(os.path.exists(self.path))


class MemberCredentialsTestCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = directory.name
        self.client = Mock()

    @staticmethod
    def _member(name):
        config = {
            "apiVersion": "v1", "kind": "Config", "current-context": name,
            "clusters": [{"name": name, "cluster": {"server": f"https://{name}.example.com"}}],
            "contexts": [{"name": name, "context": {
                "cluster": name, "user": name, "namespace": "namespace",
            }}],
            "users": [{"name": name, "user": {"exec": {
                "apiVersion": "client.authentication.k8s.io/v1beta1",
                "command": "kubelogin", "args": ["get-token", "--login", "devicecode"],
            }}}],
        }
        return {"name": name, "status": "Succeeded",
                "kubeconfig": base64.b64encode(yaml.safe_dump(config).encode("utf-8")).decode("ascii")}

    @staticmethod
    def _response(namespace, members, token=None):
        model = FleetMemberManagedNamespaceCredentialResults if namespace else FleetMemberCredentialResults
        return model.deserialize({"value": members, "continuationToken": token})

    def _invoke(self, namespace, **kwargs):
        if namespace:
            return list_namespace_member_credentials(
                _FakeCmd(), self.client, "resource-group", "fleet", "namespace", **kwargs
            )
        return list_member_credentials(_FakeCmd(), self.client, "resource-group", "fleet", **kwargs)

    @patch("azext_fleet.custom._convert_kubeconfig_to_azurecli")
    def test_merges_all_pages_and_converts_once(self, convert):
        for namespace in (False, True):
            with self.subTest(namespace=namespace):
                self.client.reset_mock()
                convert.reset_mock()
                path = os.path.join(self.directory, str(namespace))
                with open(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600), "wb") as stream:
                    stream.write(base64.b64decode(self._member("existing")["kubeconfig"]))
                self.client.list_member_credentials.side_effect = [
                    self._response(namespace, [self._member("one")], "page-2"),
                    self._response(namespace, [self._member("two")]),
                ]
                self.assertIsNone(self._invoke(namespace, path=path))
                calls = self.client.list_member_credentials.call_args_list
                self.assertEqual(len(calls), 2)
                self.assertEqual(calls[0].kwargs["body"].serialize(), {})
                self.assertEqual(calls[1].kwargs["body"].serialize(), {"continuationToken": "page-2"})
                for call in calls:
                    self.assertEqual(call.kwargs["resource_group_name"], "resource-group")
                    self.assertEqual(call.kwargs["fleet_name"], "fleet")
                    self.assertEqual(call.kwargs.get("managed_namespace_name"), "namespace" if namespace else None)
                with open(path, encoding="utf-8") as stream:
                    config = yaml.safe_load(stream)
                self.assertEqual([ctx["name"] for ctx in config["contexts"]], ["existing", "one", "two"])
                self.assertEqual(config["current-context"], "two")
                self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
                convert.assert_called_once_with(path)

    @patch("azext_fleet.custom._convert_kubeconfig_to_azurecli")
    def test_preserves_member_selection_and_starting_token(self, _convert):
        for namespace in (False, True):
            for selection in ({"member_names": ["one"]}, {"continuation_token": "start"}):
                with self.subTest(namespace=namespace, selection=selection):
                    self.client.list_member_credentials.return_value = self._response(namespace, [self._member("one")])
                    self._invoke(namespace, path=os.path.join(self.directory, "selected"),
                                 overwrite_existing=True, **selection)
                    request = self.client.list_member_credentials.call_args.kwargs["body"]
                    self.assertEqual(request.member_names, selection.get("member_names"))
                    self.assertEqual(request.continuation_token, selection.get("continuation_token"))

    @patch("azext_fleet.custom.logger")
    @patch("azext_fleet.custom._convert_kubeconfig_to_azurecli")
    def test_saves_successes_and_reports_failures_across_pages(self, convert, logger):
        for namespace in (False, True):
            with self.subTest(namespace=namespace):
                convert.reset_mock()
                logger.reset_mock()
                path = os.path.join(self.directory, str(namespace))
                failed = {"name": "bad", "status": "Failed",
                          "error": {"code": "Forbidden", "message": "Access denied"}}
                self.client.list_member_credentials.side_effect = [
                    self._response(namespace, [failed], "page-2"),
                    self._response(namespace, [self._member("good")]),
                ]
                with self.assertRaisesRegex(CLIError, "bad"):
                    self._invoke(namespace, path=path)
                with open(path, encoding="utf-8") as stream:
                    self.assertEqual(yaml.safe_load(stream)["current-context"], "good")
                convert.assert_called_once_with(path)
                self.assertIn("Access denied", str(logger.error.call_args))
                self.assertIn("Forbidden", str(logger.error.call_args))

    @patch("azext_fleet.custom._convert_kubeconfig_to_azurecli")
    def test_rejects_failed_missing_and_invalid_credentials(self, convert):
        for namespace in (False, True):
            for member in ({"name": "bad", "status": "Failed"},
                           {"name": "bad", "status": "Succeeded"},
                           {"name": "bad", "status": "Succeeded", "kubeconfig": "invalid!"},
                           {"name": "bad", "status": "Succeeded", "kubeconfig": "/w=="}):
                with self.subTest(namespace=namespace, member=member):
                    self.client.list_member_credentials.return_value = self._response(namespace, [member])
                    path = os.path.join(self.directory, "invalid")
                    with self.assertRaisesRegex(CLIError, "bad"):
                        self._invoke(namespace, path=path)
                    self.assertFalse(os.path.exists(path))
        convert.assert_not_called()

    @patch("azext_fleet.custom._convert_kubeconfig_to_azurecli")
    def test_rejects_empty_response(self, convert):
        for namespace in (False, True):
            for response in (None, self._response(namespace, [])):
                with self.subTest(namespace=namespace, response=response):
                    self.client.list_member_credentials.return_value = response
                    with self.assertRaisesRegex(CLIError, "No Kubernetes credentials"):
                        self._invoke(namespace, path=os.path.join(self.directory, "empty"))
        convert.assert_not_called()

    @patch("azext_fleet.custom._convert_kubeconfig_to_azurecli")
    def test_converts_saved_credentials_if_later_page_fails(self, convert):
        for namespace in (False, True):
            with self.subTest(namespace=namespace):
                convert.reset_mock()
                path = os.path.join(self.directory, str(namespace))
                self.client.list_member_credentials.side_effect = [
                    self._response(namespace, [self._member("one")], "next"),
                    CLIError("Service unavailable"),
                ]
                with self.assertRaisesRegex(CLIError, "Service unavailable"):
                    self._invoke(namespace, path=path)
                convert.assert_called_once_with(path)

    @patch("azext_fleet.custom._convert_kubeconfig_to_azurecli")
    def test_continues_after_empty_page(self, convert):
        for namespace in (False, True):
            with self.subTest(namespace=namespace):
                convert.reset_mock()
                path = os.path.join(self.directory, str(namespace))
                self.client.list_member_credentials.side_effect = [
                    self._response(namespace, [], "next"),
                    self._response(namespace, [self._member("one")]),
                ]
                self._invoke(namespace, path=path)
                convert.assert_called_once_with(path)

    @patch("azext_fleet.custom.logger")
    @patch("azext_fleet.custom._convert_kubeconfig_to_azurecli")
    @patch("azext_fleet.custom.print_or_merge_credentials")
    def test_continues_after_member_merge_error(self, merge, convert, logger):
        for namespace in (False, True):
            with self.subTest(namespace=namespace):
                merge.reset_mock()
                convert.reset_mock()
                logger.reset_mock()
                merge.side_effect = [CLIError("Merge conflict"), None]
                self.client.list_member_credentials.return_value = self._response(
                    namespace, [self._member("bad"), self._member("good")]
                )
                path = os.path.join(self.directory, str(namespace))
                with self.assertRaisesRegex(CLIError, "bad"):
                    self._invoke(namespace, path=path)
                self.assertEqual(merge.call_count, 2)
                self.assertIn("Merge conflict", str(logger.error.call_args))
                convert.assert_called_once_with(path)

    def test_rejects_stdout_before_calling_service(self):
        for namespace in (False, True):
            with self.subTest(namespace=namespace):
                with self.assertRaisesRegex(InvalidArgumentValueError, "--file -"):
                    self._invoke(namespace, path="-")
        self.client.list_member_credentials.assert_not_called()

    @patch("azext_fleet.custom._convert_kubeconfig_to_azurecli")
    def test_rejects_repeated_continuation_tokens(self, convert):
        for namespace in (False, True):
            with self.subTest(namespace=namespace):
                convert.reset_mock()
                path = os.path.join(self.directory, str(namespace))
                self.client.list_member_credentials.return_value = self._response(
                    namespace, [self._member("one")], "same"
                )
                with self.assertRaisesRegex(CLIError, "continuation token"):
                    self._invoke(namespace, path=path, continuation_token="same")
                convert.assert_called_once_with(path)

    @patch("azext_fleet.custom._convert_kubeconfig_to_azurecli")
    @patch("azext_fleet.custom.print_or_merge_credentials")
    def test_default_path_and_overwrite_option(self, merge, convert):
        for namespace in (False, True):
            with self.subTest(namespace=namespace):
                merge.reset_mock()
                convert.reset_mock()
                self.client.list_member_credentials.return_value = self._response(namespace, [self._member("one")])
                self._invoke(namespace, overwrite_existing=True)
                path = os.path.join(os.path.expanduser("~"), ".kube", "config")
                self.assertEqual(merge.call_args.args[0], path)
                self.assertEqual(merge.call_args.args[2:], (True, None))
                convert.assert_called_once_with(path)


if __name__ == "__main__":
    unittest.main()
