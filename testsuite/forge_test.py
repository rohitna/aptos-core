from cgitb import enable
from contextlib import ExitStack
from importlib.metadata import files
import json
import os
import unittest
import tempfile
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, Sequence, Union
from unittest.mock import patch

from . import forge
from .forge import (
    FakeTime,
    ForgeCluster,
    ForgeFormatter,
    ForgeJob,
    ForgeResult,
    ForgeState,
    GetPodsItem,
    GetPodsItemMetadata,
    GetPodsItemStatus,
    GetPodsResult,
    Git,
    K8sForgeRunner,
    ListClusterResult,
    SystemContext,
    create_forge_command,
    find_recent_images,
    find_recent_images_by_profile_or_features,
    assert_provided_image_tags_has_profile_or_features,
    format_comment,
    format_pre_comment,
    format_report,
    get_all_forge_jobs,
    get_dashboard_link,
    get_humio_logs_link,
    get_testsuite_images,
    get_validator_logs_link,
    list_eks_clusters,
    main,
    ForgeContext,
    LocalForgeRunner,
    FakeShell,
    FakeFilesystem,
    RunResult,
    FakeProcesses,
    sanitize_forge_resource_name,
)

from click.testing import CliRunner


class HasAssertMultiLineEqual(Protocol):
    def assertMultiLineEqual(self, first: str, second: str, msg: Any = ...) -> None:
        ...


def get_cwd() -> Path:
    return Path(__file__).absolute().parent


def get_fixture_path(fixture_name: str) -> Path:
    return get_cwd() / "fixtures" / fixture_name


class AssertFixtureMixin:
    def assertFixture(
        self: HasAssertMultiLineEqual, test_str: str, fixture_name: str
    ) -> None:
        fixture_path = get_fixture_path(fixture_name)
        if os.getenv("FORGE_WRITE_FIXTURES") == "true":
            print(f"Writing fixture to {str(fixture_path)}")
            fixture_path.write_text(test_str)
            fixture = test_str
        else:
            fixture = fixture_path.read_text()
        temp = Path(tempfile.mkstemp()[1])
        temp.write_text(test_str)
        self.assertMultiLineEqual(
            test_str,
            fixture,
            f"Fixture {fixture_name} does not match"
            "\n"
            f"Wrote to {str(temp)} for comparison"
            "\nRerun with FORGE_WRITE_FIXTURES=true to update the fixtures",
        )


class SpyShell(FakeShell):
    def __init__(
        self,
        command_map: Dict[str, Union[RunResult, Exception]],
        strict: bool = False,
    ) -> None:
        self.command_map = command_map
        self.commands = []
        self.strict = strict

    def run(self, command: Sequence[str], stream_output: bool = False) -> RunResult:
        rendered_command = " ".join(command)
        default = (
            Exception(f"Command not mocked: {rendered_command}")
            if self.strict
            else super().run(command)
        )
        result = self.command_map.get(rendered_command, default)
        self.commands.append(rendered_command)
        if isinstance(result, Exception):
            raise result
        return result

    async def gen_run(
        self, command: Sequence[str], stream_output: bool = False
    ) -> RunResult:
        return self.run(command, stream_output)

    def assert_commands(self, testcase) -> None:
        testcase.assertEqual(list(self.command_map.keys()), self.commands)


class SpyFilesystem(FakeFilesystem):
    def __init__(
        self,
        expected_writes: Dict[str, bytes],
        expected_reads: Dict[str, bytes],
        expected_unlinks: Optional[List[str]] = None,
    ) -> None:
        self.expected_writes = expected_writes
        self.expected_reads = expected_reads
        self.expected_unlinks = expected_unlinks or []
        self.writes = {}
        self.reads = []
        self.temp_count = 1
        self.unlinks = []

    def write(self, filename: str, contents: bytes) -> None:
        self.writes[filename] = contents

    def get_write(self, filename: str) -> bytes:
        return self.writes[filename]

    def read(self, filename: str) -> bytes:
        self.reads.append(filename)
        return self.expected_reads.get(filename, b"")

    def assert_writes(self, testcase) -> None:
        for filename, contents in self.expected_writes.items():
            testcase.assertIn(
                filename, self.writes, f"{filename} was not written: {self.writes}"
            )
            testcase.assertMultiLineEqual(
                self.writes[filename].decode(),
                contents.decode(),
                f"{filename} did not match expected contents",
            )

    def assert_reads(self, testcase) -> None:
        for filename in self.expected_reads.keys():
            testcase.assertIn(filename, self.reads, f"{filename} was not read")

    def mkstemp(self) -> str:
        filename = f"temp{self.temp_count}"
        self.temp_count += 1
        return filename

    def unlink(self, filename: str) -> None:
        self.unlinks.append(filename)

    def assert_unlinks(self, testcase) -> None:
        for filename in self.expected_unlinks:
            testcase.assertIn(filename, self.unlinks, f"{filename} was not unlinked")


class SpyProcesses(FakeProcesses):
    def run_atexit(self) -> None:
        for callback in self.exit_callbacks:
            callback()


def fake_context(
    shell=None, filesystem=None, processes=None, time=None, mode=None,
) -> ForgeContext:
    return ForgeContext(
        shell=shell if shell else FakeShell(),
        filesystem=filesystem if filesystem else FakeFilesystem(),
        processes=processes if processes else FakeProcesses(),
        time=time if time else FakeTime(),
        forge_args=create_forge_command(
            forge_runner_mode=mode,
            forge_test_suite="banana",
            forge_runner_duration_secs="123",
            forge_num_validators="10",
            forge_num_validator_fullnodes="20",
            image_tag="asdf",
            upgrade_image_tag="upgrade_asdf",
            forge_namespace="potato",
            forge_namespace_reuse="false",
            forge_namespace_keep="false",
            forge_enable_haproxy="false",
            cargo_args=["--cargo-arg"],
            forge_cli_args=["--forge-cli-arg"],
            test_args=["--test-arg"],
        ),
        aws_account_num="123",
        aws_region="banana-east-1",
        forge_image_tag="forge_asdf",
        image_tag="asdf",
        upgrade_image_tag="upgrade_asdf",
        forge_namespace="potato",
        keep_port_forwards=False,
        forge_cluster_name="tomato",
        forge_test_suite="banana",
        forge_blocking=True,
        github_actions="false",
        github_job_url="https://banana",
    )


class ForgeRunnerTests(unittest.TestCase):
    maxDiff = None

    def testLocalRunner(self) -> None:
        cargo_run = " ".join([
            "cargo", "run",
            "--cargo-arg",
            "-p", "forge-cli",
            "--",
            "--suite", "banana",
            "--duration-secs", "123",
            "--num-validators", "10",
            "--num-validator-fullnodes", "20",
            "--forge-cli-arg",
            "test", "k8s-swarm",
            "--image-tag", "asdf",
            "--upgrade-image-tag", "upgrade_asdf",
            "--namespace", "potato",
            "--port-forward",
            "--test-arg"
        ])
        shell = SpyShell(
            OrderedDict(
                [
                    (cargo_run, RunResult(0, b"orange"),),
                    ("kubectl get pods -n potato", RunResult(0, b"Pods")),
                ]
            )
        )
        filesystem = SpyFilesystem({}, {})
        context = fake_context(shell, filesystem, mode="local")
        runner = LocalForgeRunner()
        result = runner.run(context)
        self.assertEqual(result.state, ForgeState.PASS, result.output)
        shell.assert_commands(self)
        filesystem.assert_writes(self)
        filesystem.assert_reads(self)

    def testK8sRunner(self) -> None:
        self.maxDiff = None
        shell = SpyShell(
            OrderedDict(
                [
                    (
                        "kubectl delete pod -n default -l forge-namespace=potato --force",
                        RunResult(0, b""),
                    ),
                    (
                        "kubectl wait -n default --for=delete pod -l forge-namespace=potato",
                        RunResult(0, b""),
                    ),
                    ("kubectl apply -n default -f temp1", RunResult(0, b"")),
                    (
                        "kubectl wait -n default --timeout=5m --for=condition=Ready pod/potato-1659078000-asdf",
                        RunResult(0, b""),
                    ),
                    (
                        "kubectl logs -n default -f potato-1659078000-asdf",
                        RunResult(0, b""),
                    ),
                    (
                        "kubectl get pod -n default potato-1659078000-asdf -o jsonpath='{.status.phase}'",
                        RunResult(0, b"Succeeded"),
                    ),
                    ("kubectl get pods -n potato", RunResult(0, b"Pods")),
                ]
            )
        )
        forge_yaml = get_cwd() / "forge-test-runner-template.yaml"
        template_fixture = get_fixture_path("forge-test-runner-template.fixture")
        filesystem = SpyFilesystem(
            {
                "temp1": template_fixture.read_bytes(),
            },
            {
                "testsuite/forge-test-runner-template.yaml": forge_yaml.read_bytes(),
            },
        )
        context = fake_context(shell, filesystem, mode="k8s")
        runner = K8sForgeRunner()
        result = runner.run(context)
        shell.assert_commands(self)
        filesystem.assert_writes(self)
        filesystem.assert_reads(self)
        self.assertEqual(result.state, ForgeState.PASS, result.output)


class TestFindRecentImage(unittest.TestCase):
    def testFindRecentImage(self) -> None:
        shell = SpyShell(
            OrderedDict(
                [
                    ("git rev-parse HEAD~0", RunResult(0, b"potato\n")),
                    (
                        "aws ecr describe-images --repository-name aptos/validator --image-ids imageTag=potato",
                        RunResult(1, b""),
                    ),
                    ("git rev-parse HEAD~1", RunResult(0, b"lychee\n")),
                    (
                        "aws ecr describe-images --repository-name aptos/validator --image-ids imageTag=lychee",
                        RunResult(0, b""),
                    ),
                ]
            )
        )
        git = Git(shell)
        image_tags = find_recent_images(shell, git, 1, "aptos/validator")
        self.assertEqual(list(image_tags), ["lychee"])
        shell.assert_commands(self)

    def testFindRecentFailpointsImage(self) -> None:
        shell = SpyShell(
            OrderedDict(
                [
                    ("git rev-parse HEAD~0", RunResult(0, b"tomato\n")),
                    (
                        "aws ecr describe-images --repository-name aptos/validator --image-ids imageTag=failpoints_tomato",
                        RunResult(0, b""),
                    ),
                ]
            )
        )
        git = Git(shell)
        image_tags = find_recent_images_by_profile_or_features(
            shell,
            git,
            1,
            enable_performance_profile=False,
            enable_failpoints_feature=True
        )
        self.assertEqual(list(image_tags), ["failpoints_tomato"])
        shell.assert_commands(self)

    def testFindRecentPerformanceImage(self) -> None:
        shell = SpyShell(
            OrderedDict(
                [
                    ("git rev-parse HEAD~0", RunResult(0, b"potato\n")),
                    (
                        "aws ecr describe-images --repository-name aptos/validator --image-ids imageTag=performance_potato",
                        RunResult(0, b""),
                    ),
                ]
            )
        )
        git = Git(shell)
        image_tags = find_recent_images_by_profile_or_features(
            shell,
            git,
            1,
            enable_performance_profile=True,
            enable_failpoints_feature=False,
        )
        self.assertEqual(list(image_tags), ["performance_potato"])
        shell.assert_commands(self)

    def testFailBothFailpointsPerformance(self) -> None:
        shell = SpyShell(OrderedDict())
        git = Git(shell)
        with self.assertRaises(Exception):
            find_recent_images_by_profile_or_features(
                shell,
                git,
                1,
                enable_performance_profile=True,
                enable_failpoints_feature=True,
            )

    def testDidntFindRecentImage(self) -> None:
        shell = SpyShell(
            OrderedDict(
                [
                    ("git rev-parse HEAD~0", RunResult(0, b"crab\n")),
                    (
                        "aws ecr describe-images --repository-name aptos/validator --image-ids imageTag=crab",
                        RunResult(1, b""),
                    ),
                ]
            )
        )
        git = Git(shell)
        with self.assertRaises(Exception):
            list(
                find_recent_images(
                    shell, git, 1, "aptos/validator", commit_threshold=1
                )
            )

    def testFailpointsProvidedImageTag(self) -> None:
        with self.assertRaises(AssertionError):
            assert_provided_image_tags_has_profile_or_features(
                "potato_tomato",
                "failpoints_performance_potato",
                enable_failpoints_feature=True,
                enable_performance_profile=False,
            )

    def testFailpointsNoProvidedImageTag(self) -> None:
        assert_provided_image_tags_has_profile_or_features(
            None,
            None,
            enable_failpoints_feature=True,
            enable_performance_profile=False,
        )


class ForgeFormattingTests(unittest.TestCase, AssertFixtureMixin):
    maxDiff = None

    def testTestsuiteImages(self) -> None:
        context = fake_context()
        # set the image tag and upgrade image tag to the same value
        upgrade_img = context.upgrade_image_tag
        context.upgrade_image_tag = context.image_tag
        # do not expect an upgrade
        txt = get_testsuite_images(context)
        self.assertEqual(txt, f"`{context.image_tag}`")

        # upgrade
        context.upgrade_image_tag = upgrade_img
        txt = get_testsuite_images(context)
        self.assertEqual(txt, f"`{context.image_tag}` ==> `{context.upgrade_image_tag}`")

    def testReport(self) -> None:
        filesystem = SpyFilesystem({"test": b"banana"}, {})
        context = fake_context(filesystem=filesystem)
        result = ForgeResult.from_args(ForgeState.PASS, "test")
        context.report(result, [ForgeFormatter("test", lambda c, r: "banana")])
        filesystem.assert_reads(self)
        filesystem.assert_writes(self)

    def testHumioLogLink(self) -> None:
        link = get_humio_logs_link("forge-pr-2983")
        self.assertFixture(link, "testHumioLogLink.fixture")
        self.assertIn("forge-pr-2983", link)

    def testValidatorLogsLink(self) -> None:
        self.assertFixture(
            get_validator_logs_link("aptos-perry", "perrynet", True),
            "testValidatorLogsLink.fixture",
        )

    def testDashboardLinkAutoRefresh(self) -> None:
        self.assertFixture(
            get_dashboard_link(
                "aptos-forge-big-1",
                "forge-pr-2983",
                "forge-big-1",
                True,
            ),
            "testDashboardLinkAutoRefresh.fixture",
        )

    def testDashboardLinkTimeInterval(self) -> None:
        self.assertFixture(
            get_dashboard_link(
                "aptos-forge-big-1",
                "forge-pr-2983",
                "forge-big-1",
                (
                    datetime.fromtimestamp(100000, timezone.utc),
                    datetime.fromtimestamp(100001, timezone.utc),
                ),
            ),
            "testDashboardLinkTimeInterval.fixture",
        )

    def testFormatPreComment(self) -> None:
        context = fake_context()
        self.assertFixture(
            format_pre_comment(context),
            "testFormatPreComment.fixture",
        )

    def testFormatComment(self) -> None:
        context = fake_context()
        report_fixture = get_fixture_path("report.fixture")
        with ForgeResult.with_context(context) as forge_result:
            forge_result.set_state(ForgeState.PASS)
            forge_result.set_output(report_fixture.read_text())
        self.assertFixture(
            format_comment(context, forge_result),
            "testFormatComment.fixture",
        )

    def testFormatReport(self) -> None:
        context = fake_context()
        report_fixture = get_fixture_path("report.fixture")
        with ForgeResult.with_context(context) as forge_result:
            forge_result.set_state(ForgeState.PASS)
            forge_result.set_output(report_fixture.read_text())
        self.assertFixture(
            format_report(context, forge_result),
            "testFormatReport.fixture",
        )

    def testSanitizeForgeNamespaceSlashes(self) -> None:
        namespace_with_slash = "banana/apple"
        namespace = sanitize_forge_resource_name(namespace_with_slash)
        self.assertEqual(namespace, "banana-apple")

    def testSanitizeForgeNamespaceTooLong(self) -> None:
        namespace_too_long = "a" * 10000
        namespace = sanitize_forge_resource_name(namespace_too_long)
        self.assertEqual(namespace, "a" * 64)


class ForgeMainTests(unittest.TestCase, AssertFixtureMixin):
    maxDiff = None

    def testMain(self) -> None:
        runner = CliRunner()
        shell = SpyShell(OrderedDict([
            ('aws sts get-caller-identity', RunResult(0, b'{"Account": "123456789012"}')),
            ('kubectl config current-context', RunResult(0, b'aptos-banana')),
            ('git rev-parse HEAD~0', RunResult(0, b'banana')),
            (
                'aws ecr describe-images --repository-name aptos/validator --im'
                'age-ids imageTag=banana',
                RunResult(0, b''),
            ),
            ('aws eks update-kubeconfig --name forge-big-1', RunResult(0, b'')),
            (
                'kubectl delete pod -n default -l forge-namespace=forge-perry-1659078000 '
                '--force',
                RunResult(0, b''),
            ),
            (
                'kubectl wait -n default --for=delete pod -l '
                'forge-namespace=forge-perry-1659078000',
                RunResult(0, b''),
            ),
            (
                'kubectl apply -n default -f temp1',
                RunResult(0, b''),
            ),
            (
                'kubectl wait -n default --timeout=5m --for=condition=Ready '
                'pod/forge-perry-1659078000-1659078000-banana',
                RunResult(0, b''),
            ),
            (
                'kubectl logs -n default -f forge-perry-1659078000-1659078000-banana',
                RunResult(0, b''),
            ),
            (
                'kubectl get pod -n default forge-perry-1659078000-1659078000-banana -o '
                "jsonpath='{.status.phase}'",
                RunResult(0, b''),
            ),
            (
                'kubectl get pods -n forge-perry-1659078000',
                RunResult(0, b''),
            )
        ]))
        filesystem = SpyFilesystem({
            "temp-comment": get_fixture_path("testMainComment.fixture").read_bytes(),
            "temp-step-summary": get_fixture_path("testMainComment.fixture").read_bytes(),
            "temp-pre-comment": get_fixture_path("testMainPreComment.fixture").read_bytes(),
            "temp-report": get_fixture_path("testMainReport.fixture").read_bytes(),
        }, {})
        with ExitStack() as stack:
            stack.enter_context(runner.isolated_filesystem())
            stack.enter_context(patch.object(forge, "FakeFilesystem", lambda: filesystem))
            stack.enter_context(patch.object(forge, "FakeShell", lambda: shell))

            os.mkdir(".git")
            os.mkdir("testsuite")
            template_name = "forge-test-runner-template.yaml"
            Path(f"testsuite/{template_name}").write_text(
                (Path(__file__).parent / template_name).read_text()
            )
            result = runner.invoke(
                main,
                [
                    "test",
                    "--dry-run",
                    "--forge-cluster-name", "forge-big-1",
                    "--forge-report", "temp-report",
                    "--forge-pre-comment", "temp-pre-comment",
                    "--forge-comment", "temp-comment",
                    "--github-step-summary", "temp-step-summary",
                    "--github-server-url", "None",
                    "--github-repository", "None",
                    "--github-run-id", "None",
                ],
                catch_exceptions=False,
            )
            shell.assert_commands(self)
            self.assertFixture(filesystem.get_write("temp-comment").decode(), "testMainComment.fixture")
            self.assertFixture(filesystem.get_write("temp-step-summary").decode(), "testMainComment.fixture")
            self.assertFixture(filesystem.get_write("temp-pre-comment").decode(), "testMainPreComment.fixture")
            self.assertFixture(filesystem.get_write("temp-report").decode(), "testMainReport.fixture")
            self.assertFixture(result.output, "testMain.fixture")


class TestListClusters(unittest.TestCase):
    def testListClusters(self) -> None:
        fake_clusters = json.dumps(
            ListClusterResult(
                clusters=[
                    "banana-fake-1",
                    "aptos-forge-banana-1",
                    "aptos-forge-potato-2",
                ]
            ),
        )
        shell = SpyShell(
            OrderedDict(
                [
                    ("aws eks list-clusters", RunResult(0, fake_clusters.encode())),
                ]
            )
        )
        clusters = list_eks_clusters(shell)
        self.assertEqual(clusters, ["aptos-forge-banana-1", "aptos-forge-potato-2"])
        shell.assert_commands(self)

    def testListClustersFails(self) -> None:
        with self.assertRaises(Exception):
            shell = SpyShell(
                OrderedDict(
                    [
                        ("Blah", RunResult(0, b"")),
                    ]
                )
            )
            list_eks_clusters(shell)
            shell.assert_commands(self)


def fake_pod_item(name: str, phase: str) -> GetPodsItem:
    return GetPodsItem(
        metadata=GetPodsItemMetadata(name=name), status=GetPodsItemStatus(phase=phase)
    )


class GetForgeJobsTests(unittest.IsolatedAsyncioTestCase):
    async def testGetAllForgeJobs(self) -> None:
        fake_clusters = json.dumps(
            ListClusterResult(
                clusters=["aptos-forge-banana", "banana-1", "aptos-forge-apple-2"]
            )
        ).encode()
        fake_first_pods = GetPodsResult(
            items=[
                fake_pod_item("forge-first", "Running"),
                fake_pod_item("forge-failed", "Failed"),
                fake_pod_item("ignore-me", "Failed"),
            ]
        )
        fake_second_pods = GetPodsResult(
            items=[
                fake_pod_item("forge-second", "Running"),
                fake_pod_item("forge-succeeded", "Succeeded"),
                fake_pod_item("me-too", "Failed"),
            ]
        )
        shell = SpyShell(
            OrderedDict(
                [
                    ("aws eks list-clusters", RunResult(0, fake_clusters)),
                    (
                        "aws eks update-kubeconfig --name aptos-forge-banana --kubeconfig temp1",
                        RunResult(0, b""),
                    ),
                    (
                        "kubectl get pods -n default --kubeconfig temp1 -o json",
                        RunResult(0, json.dumps(fake_first_pods).encode()),
                    ),
                    (
                        "aws eks update-kubeconfig --name aptos-forge-apple-2 --kubeconfig temp2",
                        RunResult(0, b""),
                    ),
                    (
                        "kubectl get pods -n default --kubeconfig temp2 -o json",
                        RunResult(0, json.dumps(fake_second_pods).encode()),
                    ),
                ]
            ),
            strict=True,
        )
        filesystem = SpyFilesystem({}, {}, ["temp1", "temp2"])
        processes = SpyProcesses()
        context = SystemContext(shell, filesystem, processes)
        jobs = await get_all_forge_jobs(context)
        expected_jobs = [
            ForgeJob(
                name="forge-first",
                phase="Running",
                cluster=ForgeCluster(
                    name="aptos-forge-banana",
                    kubeconf="temp1",
                ),
            ),
            ForgeJob(
                name="forge-failed",
                phase="Failed",
                cluster=ForgeCluster(
                    name="aptos-forge-banana",
                    kubeconf="temp1",
                ),
            ),
            ForgeJob(
                name="forge-second",
                phase="Running",
                cluster=ForgeCluster(
                    name="aptos-forge-apple-2",
                    kubeconf="temp2",
                ),
            ),
            ForgeJob(
                name="forge-succeeded",
                phase="Succeeded",
                cluster=ForgeCluster(
                    name="aptos-forge-apple-2",
                    kubeconf="temp2",
                ),
            ),
        ]
        self.assertEqual(jobs, expected_jobs)
        processes.run_atexit()
        filesystem.assert_unlinks(self)
