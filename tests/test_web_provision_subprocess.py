from __future__ import annotations

import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from core.disk import DiskProvisioner
from core.downloader import MacOSDownloader
from core import reims_util
from core.web_job_runner import JobCancelledError, JobTimeoutError


class RecordingJobContext:
    def __init__(self, *, error=None):
        self.calls = []
        self.error = error

    def run_subprocess(self, argv, *, timeout=None, check=True):
        self.calls.append((list(argv), timeout, check))
        if self.error:
            raise self.error
        if argv[0] == "dmg2img":
            Path(argv[-1]).write_bytes(b"image")
        elif len(argv) > 1 and argv[1] == "create":
            Path(argv[-2]).write_bytes(b"qcow2-complete")
        return subprocess.CompletedProcess(list(argv), 0)


class FakeDownloadResponse:
    def __init__(self, data):
        self.data = data
        self.headers = {"Content-Length": str(len(data))}

    def read(self, _size=-1):
        data, self.data = self.data, b""
        return data


class CancelAfterFirstDownloadChunk:
    def __init__(self):
        self.checks = 0

    def check_cancelled(self):
        self.checks += 1
        if self.checks == 3:
            raise JobCancelledError("cancelled during download")

    def phase(self, *_args, **_kwargs):
        return None


class NoopDownloadContext:
    def check_cancelled(self):
        return None

    def checkpoint(self, *_args, **_kwargs):
        return None

    def phase(self, *_args, **_kwargs):
        return None


class ReimsJobContext:
    def __init__(self, *, interrupt=False):
        self.interrupt = interrupt

    def run_subprocess(self, argv, *, timeout=None, check=True):
        if argv[1] == "opencore":
            target = Path(argv[argv.index("-o") + 1])
            target.write_bytes(b"partial" if self.interrupt else b"complete-efi")
            if self.interrupt:
                raise JobCancelledError("cancelled during OpenCore generation")
        return subprocess.CompletedProcess(list(argv), 0)


class WebProvisionSubprocessTests(unittest.TestCase):
    def test_installer_errors_are_quiet_only_for_headless_calls(self):
        with patch("core.downloader.console.print", side_effect=AssertionError("Rich output in headless job")), \
             redirect_stdout(StringIO()) as stdout, redirect_stderr(StringIO()) as stderr:
            self.assertIsNone(MacOSDownloader.prepare_installer("unsupported", job_context=NoopDownloadContext()))
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")

        with patch("core.downloader.console.print") as print_rich:
            self.assertIsNone(MacOSDownloader.prepare_installer("unsupported"))
        print_rich.assert_called_once()

    def test_headless_download_skips_rich_and_tui_download_keeps_rich_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "BaseSystem.dmg"
            open_response = lambda *_args, **_kwargs: FakeDownloadResponse(b"payload")
            with patch("core.downloader.urlopen", side_effect=open_response), \
                 patch("core.downloader.Progress", side_effect=AssertionError("Rich progress in headless job")), \
                 patch("core.downloader.console.print", side_effect=AssertionError("Rich output in headless job")), \
                 redirect_stdout(StringIO()) as stdout, redirect_stderr(StringIO()) as stderr:
                self.assertTrue(MacOSDownloader._stream_download(
                    "https://example.test/file", "token", destination, "headless",
                    job_context=NoopDownloadContext(),
                ))
            self.assertEqual(stdout.getvalue(), "")
            self.assertEqual(stderr.getvalue(), "")
            self.assertEqual(destination.read_bytes(), b"payload")

            rich_progress = unittest.mock.MagicMock()
            rich_progress.__enter__.return_value = rich_progress
            with patch("core.downloader.urlopen", side_effect=open_response), \
                 patch("core.downloader.Progress", return_value=rich_progress) as progress, \
                 redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                self.assertTrue(MacOSDownloader._stream_download(
                    "https://example.test/file", "token", destination, "TUI",
                ))
            progress.assert_called_once()

    def test_disk_provisioning_skips_rich_for_job_but_retains_tui_progress(self):
        def exercise(job_context):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                vm_dir = root / "test-vm"
                code = root / "OVMF_CODE.fd"
                vars_template = root / "OVMF_VARS.template.fd"
                code.touch()
                vars_template.write_bytes(b"fresh-nvram-template")
                vm = SimpleNamespace(
                    id="test-vm", disk_size_gb=40, smbios=SimpleNamespace(
                        serial_number="serial", board_serial="board", smuuid="uuid", rom="rom"
                    ), macos_version="sequoia", opencore_show_picker=False, disk_path="",
                )
                fake_progress = unittest.mock.MagicMock()
                fake_progress.__enter__.return_value = fake_progress
                with patch("core.disk.VM_STORAGE_BASE", root), \
                     patch("core.disk.DEFAULT_OVMF_CODE_TEMPLATES", [code]), \
                     patch("core.disk.DEFAULT_OVMF_VARS_TEMPLATES", [vars_template]), \
                     patch.object(DiskProvisioner, "_create_qcow2_disk", side_effect=lambda path, *_a, **_k: Path(path).touch()), \
                     patch.object(DiskProvisioner, "ensure_ovmf_code", return_value=True), \
                     patch("core.disk.GenSMBIOS.validate"), \
                     patch("core.disk.reims_util.generate_opencore", side_effect=lambda path, *_a, **_k: (Path(path).touch() or True)), \
                     patch("core.disk.console.print", side_effect=AssertionError("Rich output in headless job")):
                    if job_context is not None:
                        with patch("core.disk.Progress", side_effect=AssertionError("Rich progress in headless job")), \
                             redirect_stdout(StringIO()) as stdout, redirect_stderr(StringIO()) as stderr:
                            DiskProvisioner.provision(vm, job_context=job_context)
                        self.assertEqual(stdout.getvalue(), "")
                        self.assertEqual(stderr.getvalue(), "")
                        self.assertEqual(
                            (vm_dir / "OVMF_VARS.fd").read_bytes(),
                            b"fresh-nvram-template",
                        )
                    else:
                        with patch("core.disk.Progress", return_value=fake_progress) as progress:
                            bundle = DiskProvisioner.provision(vm)
                        progress.assert_called_once()
                        self.assertEqual(
                            (bundle.vm_dir / "OVMF_VARS.fd").read_bytes(),
                            b"fresh-nvram-template",
                        )

        exercise(NoopDownloadContext())
        exercise(None)

    def test_qcow2_creation_uses_job_supervisor_when_context_is_supplied(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "disk.qcow2"
            context = RecordingJobContext()
            with patch("core.disk.subprocess.run", side_effect=AssertionError("unmanaged subprocess")):
                DiskProvisioner._create_qcow2_disk(target, 40, job_context=context)
            self.assertTrue(target.is_file())
        self.assertEqual(len(context.calls), 2)
        argv, timeout, check = context.calls[0]
        self.assertEqual(argv[0], "qemu-img")
        self.assertEqual(argv[1], "create")
        self.assertEqual(timeout, None)
        self.assertFalse(check)
        self.assertEqual(context.calls[1][0][1], "check")

    def test_interrupted_disk_is_not_published_and_retry_creates_final_disk(self):
        class InterruptedContext(RecordingJobContext):
            def run_subprocess(self, argv, *, timeout=None, check=True):
                self.calls.append((list(argv), timeout, check))
                if argv[1] == "create":
                    Path(argv[-2]).write_bytes(b"partial")
                    raise JobCancelledError("cancelled during qemu-img create")
                return subprocess.CompletedProcess(list(argv), 0)

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "disk.qcow2"
            with self.assertRaisesRegex(JobCancelledError, "cancelled during qemu-img"):
                DiskProvisioner._create_qcow2_disk(target, 40, job_context=InterruptedContext())
            self.assertFalse(target.exists())
            self.assertEqual(list(Path(directory).iterdir()), [])

            DiskProvisioner._create_qcow2_disk(target, 40, job_context=RecordingJobContext())
            self.assertEqual(target.read_bytes(), b"qcow2-complete")

    def test_dmg_conversion_uses_job_supervisor_and_preserves_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "BaseSystem.dmg"
            target = Path(directory) / "BaseSystem.img"
            source.touch()
            context = RecordingJobContext()
            with patch("core.downloader.subprocess.run", side_effect=AssertionError("unmanaged subprocess")):
                self.assertTrue(MacOSDownloader._convert_dmg_to_img(source, target, job_context=context))
            self.assertEqual(context.calls[0][0][0], "dmg2img")

            cancelled = RecordingJobContext(error=JobTimeoutError("conversion timeout"))
            with self.assertRaisesRegex(JobTimeoutError, "conversion timeout"):
                MacOSDownloader._convert_dmg_to_img(source, target, job_context=cancelled)

    def test_interrupted_conversion_is_not_published_and_retry_replaces_it(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "BaseSystem.dmg"
            target = Path(directory) / "BaseSystem.img"
            source.touch()
            target.write_bytes(b"stale-partial")
            interrupted = RecordingJobContext(error=JobCancelledError("cancelled during conversion"))
            with self.assertRaisesRegex(JobCancelledError, "cancelled during conversion"):
                MacOSDownloader._convert_dmg_to_img(source, target, job_context=interrupted)
            self.assertEqual(target.read_bytes(), b"stale-partial")
            self.assertEqual(sorted(path.name for path in Path(directory).iterdir()), ["BaseSystem.dmg", "BaseSystem.img"])

            self.assertTrue(MacOSDownloader._convert_dmg_to_img(source, target, job_context=RecordingJobContext()))
            self.assertEqual(target.read_bytes(), b"image")

    def test_interrupted_download_is_not_published_and_retry_downloads_complete_asset(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "BaseSystem.dmg"
            urlopen = lambda *_args, **_kwargs: FakeDownloadResponse(b"complete")
            with patch("core.downloader.urlopen", side_effect=urlopen):
                with self.assertRaisesRegex(JobCancelledError, "cancelled during download"):
                    MacOSDownloader._stream_download(
                        "https://example.test/file", "token", destination, "test",
                        job_context=CancelAfterFirstDownloadChunk(),
                    )
                self.assertFalse(destination.exists())
                self.assertEqual(list(Path(directory).iterdir()), [])

                self.assertTrue(MacOSDownloader._stream_download(
                    "https://example.test/file", "token", destination, "test",
                    job_context=NoopDownloadContext(),
                ))
            self.assertEqual(destination.read_bytes(), b"complete")

    def test_interrupted_opencore_generation_is_not_published_and_retry_is_atomic(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "OpenCore.qcow2"
            log_dir = Path(directory) / "logs"
            log_dir.mkdir()
            smbios = SimpleNamespace(
                model="iMac20,1", serial_number="C02TEST", board_serial="C02BOARD",
                smuuid="00000000-0000-4000-8000-000000000001", rom="02AABBCCDDEE",
            )
            common_patches = (
                patch("core.reims_util.find_reims_util", return_value=Path("/fake/reims-util")),
                patch("core.reims_util.LOGS_DIR", log_dir),
                patch("core.reims_util.LOG_FILE", log_dir / "reims.log"),
                patch("core.reims_util.shutil.which", return_value="/fake/qemu-img"),
                patch("core.reims_util.subprocess.run", return_value=SimpleNamespace(
                    returncode=0, stdout="--no-apv-arm64-port", stderr=""
                )),
            )
            with common_patches[0], common_patches[1], common_patches[2], common_patches[3], common_patches[4]:
                with self.assertRaisesRegex(JobCancelledError, "cancelled during OpenCore"):
                    reims_util.generate_opencore(output, smbios, job_context=ReimsJobContext(interrupt=True))
                self.assertFalse(output.exists())
                self.assertEqual(list(Path(directory).glob(".*.qcow2")), [])

                self.assertTrue(reims_util.generate_opencore(output, smbios, job_context=ReimsJobContext()))
            self.assertEqual(output.read_bytes(), b"complete-efi")


if __name__ == "__main__":
    unittest.main()
