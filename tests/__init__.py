-quiz-archive-worker-local/tests/__init__.py        �Gandraß <niels@gandrass.de>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
import os
import shutil
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Tuple, List, Dict, Union
from unittest.mock import patch
from uuid import UUID

import pytest

from archiveworker.custom_types import JobStatus, BackupStatus, WorkerThreadInterrupter
from archiveworker.moodle_quiz_archive_worker import app as original_app, job_queue, job_history, InterruptableThread
from config import Config


@pytest.fixture()
def app():
    app = original_app
    app.config.update({
        "TESTING": True,
    })

    # Kill all still existing threads
    for t in threading.enumerate():
        if isinstance(t, InterruptableThread):
            print(f"Cleaning up thread: {t.name} ...", end='')
            t.stop()
            job_queue.put_nowait(WorkerThreadInterrupter())
            t.join()
            print(' OK.')

    # Ensure an empty queue and history on each run
    job_queue.queue.clear()
    job_history.clear()

    # Enforce some config values for tests
    Config.UNIT_TESTS_RUNNING = True
    Config.REPORT_WAIT_FOR_READY_SIGNAL = False
    Config.REQUEST_TIMEOUT_SEC = 30

    yield app


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def runner(app):
    return app.test_cli_runner()


class TestUtils:
    """
    Util function for tests
    """

    @classmethod
    def assert_is_file_with_size(cls, file: Union[str, Path], min_size: int = None, max_size: int = None) -> None:
        """
        Asserts that the file exists and has the expected size.

        :param file: Path to file
        :param min_size: Minimum expected file size in bytes
        :param max_size: Maximum expected file size in bytes
        :return: None
        """
        assert os.path.isfile(file), f"File not found: {file}"

        fsize = os.path.getsize(file)
        if min_size:
            assert fsize >= min_size, f"File size too small: {fsize} bytes (at least {min_size} bytes required)"
        if max_size:
            assert fsize <= max_size, f"File size too large: {fsize} bytes (max {max_size} bytes allowed)"


class MoodleAPIMockBase:
    """
    Base class for Moodle API mocks
    """

    CLS_ROOT = 'archiveworker.moodle_api.MoodleAPI'

    def __init__(self):
        self.upload_tempdir = None
        self.uploaded_files = {}
        self.upload_fileid_ptr = 1
        self.patchers = {
            'check_connection': patch(self.CLS_ROOT+'.check_connection', new=self.check_connection),
            'update_job_status': patch(self.CLS_ROOT+'.update_job_status', new=self.update_job_status),
            'get_backup_status': patch(self.CLS_ROOT+'.get_backup_status', new=self.get_backup_status),
            'get_remote_file_metadata': patch(self.CLS_ROOT+'.get_remote_file_metadata', new=self.get_remote_file_metadata),
            'download_moodle_file': patch(self.CLS_ROOT+'.download_moodle_file', new=self.download_moodle_file),
            'get_attempts_metadata': patch(self.CLS_ROOT+'.get_attempts_metadata', new=self.get_attempts_metadata),
            'get_attempt_data': patch(self.CLS_ROOT+'.get_attempt_data', new=self.get_attempt_data),
            'upload_file': patch(self.CLS_ROOT+'.upload_file', new=self.upload_file),
            'process_uploaded_artifact': patch(self.CLS_ROOT+'.process_uploaded_artifact', new=self.process_uploaded_artifact),
        }

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()

    def __del__(self):
        self.stop()

    def start(self) -> None:
        """
        Start all patchers. Calls to patched functions will be redirected to the
        mock methods, defined in this class.

        :return: None
        """
        self.upload_tempdir = tempfile.TemporaryDirectory()
        self.uploaded_files = {}
        self.upload_fileid_ptr = 1

        for p in self.patchers.values():
            p.start()

    def stop(self) -> None:
        """
        Stop all patchers. Calls to patched functions will be redirected to the
        original methods.

        :return: None
        """
        if self.upload_tempdir:
            self.upload_tempdir.cleanup()
            self.upload_tempdir = None

        for p in self.patchers.values():
            p.stop()

    def get_uploaded_files(self) -> Dict[int, Dict[str, Union[str, Path]]]:
        """
        Retrieves the metadata array with pointers to all uploaded files.

        Note: Files are uploaded to a temporary directory that is deleted when
        the mocking is stopped or this object is destroyed.

        :return: Dict of uploaded files
        """
        return self.uploaded_files

    # v-- API function mocks below --v

    def check_connection(self) -> bool:
        return True

    def update_job_status(self, jobid: UUID, status: JobStatus, statusextras: Dict) -> bool:
        return True

    def get_backup_status(self, jobid: UUID, backupid: str) -> BackupStatus:
        return BackupStatus.SUCCESS

    def get_remote_file_metadata(self, download_url: str) -> Tuple[str, int]:
        return 'application/vnd.moodle.backup', 1048576

    def download_moodle_file(
            self,
            download_url: str,
            target_path: Path,
            target_filename: str,
            sha1sum_expected: str = None,
            maxsize_bytes: int = Config.DOWNLOAD_MAX_FILESIZE_BYTES
    ) -> int:
        raise NotImplementedError('download_moodle_file')

    def get_attempts_metadata(self, courseid: int, cmid: int, quizid: int, attemptids: List[int]) -> List[Dict[str, str]]:
        raise NotImplementedError('get_attempts_metadata')

    def get_attempt_data(
            self,
            courseid: int,
            cmid: int,
            quizid: int,
            attemptid: int,
            sections: dict,
            filenamepattern: str,
            attachments: bool
    ) -> Tuple[str, str, List[Dict[str, str]]]:
        raise NotImplementedError('get_attempt_data')

    def upload_file(self, file: Path) -> Dict[str, str]:
        if not file.is_file():
            raise FileNotFoundError(f'File not found: {file}')

        # Copy file to local tempdir
        pathuuid = uuid.uuid4().hex
        target_path = os.path.join(self.upload_tempdir.name, pathuuid)
        os.makedirs(target_path)

        target_file = Path(os.path.join(target_path, file.name))
        shutil.copy2(file, target_file)

        # Store file metadata and generate Moodle-ish response. The field itemid
        # corresponds to the index inside self.uploaded_files.
        self.uploaded_files[self.upload_fileid_ptr] = {
            'file': target_file,
            'metadata': {
                'component': 'user',
                'contextid': 1,
                'userid': 2,
                'filearea': 'draft',
                'filename': file.name,
                'filepath': '/',
                'itemid': self.upload_fileid_ptr,
            },
        }
        self.upload_fileid_ptr += 1

        return self.uploaded_files[self.upload_fileid_ptr - 1]['metadata']

    def process_uploaded_artifact(
            self,
            jobid: UUID,
            component: str,
            contextid: int,
            userid: int,
            filearea: str,
            filename: str,
            filepath: str,
            itemid: int,
            sha256sum: str
    ) -> bool:
        return True
;
        }
    }

    console.log(SIGNAL_PAGE_READY_FOR_EXPORT);
}

// Ignite the readiness detection process.
setTimeout(function() {
    detectAndPrepareReadinessComponents();
    checkReadiness();
}, 1000);
03b36437"},
    {file = "greenlet-3.1.1-cp39-cp39-manylinux_2_17_x86_64.manylinux2014_x86_64.whl", hash = "sha256:73aaad12ac0ff500f62cebed98d8789198ea0e6f233421059fa68a5aa7220145"},
    {file = "greenlet-3.1.1-cp39-cp39-manylinux_2_24_x86_64.manylinux_2_28_x86_64.whl", hash = "sha256:63e4844797b975b9af3a3fb8f7866ff08775f5426925e1e0bbcfe7932059a12c"},
    {file = "greenlet-3.1.1-cp39-cp39-musllinux_1_1_aarch64.whl", hash = "sha256:7939aa3ca7d2a1593596e7ac6d59391ff30281ef280d8632fa03d81f7c5f955e"},
    {file = "greenlet-3.1.1-cp39-cp39-musllinux_1_1_x86_64.whl", hash = "sha256:d0028e725ee18175c6e422797c407874da24381ce0690d6b9396c204c7f7276e"},
    {file = "greenlet-3.1.1-cp39-cp39-win32.whl", hash = "sha256:5e06afd14cbaf9e00899fae69b24a32f2196c19de08fcb9f4779dd4f004e5e7c"},
    {file = "greenlet-3.1.1-cp39-cp39-win_amd64.whl", hash = "sha256:3319aa75e0e0639bc15ff54ca327e8dc7a6fe404003496e3c6925cd3142e0e22"},
    {file = "greenlet-3.1.1.tar.gz", hash = "sha256:4ce3ac6cdb6adf7946475d7ef31777c26d94bccc377e070a7986bd2d5c515467"},
]

[package.extras]
docs = ["Sphinx", "furo"]
test = ["objgraph", "psutil"]

[[package]]
name = "idna"
version = "3.10"
description = "Internationalized Domain Names in Applications (IDNA)"
optional = false
python-versions = ">=3.6"
files = [
    {file = "idna-3.10-py3-none-any.whl", hash = "sha256:946d195a0d259cbba61165e88e65941f16e9b36ea6ddb97f00452bae8b1287d3"},
    {file = "idna-3.10.tar.gz", hash = "sha256:12f65c9b470abda6dc35cf8e63cc574b1c52b11df2c86030af0ac09b01b13ea9"},
]

[package.extras]
all = ["flake8 (>=7.1.1)", "mypy (>=1.11.2)", "pytest (>=8.3.2)", "ruff (>=0.6.2)"]

[[package]]
name = "iniconfig"
version = "2.0.0"
description = "brain-dead simple config-ini parsing"
optional = false
python-versions = ">=3.7"
files = [
    {file = "iniconfig-2.0.0-py3-none-any.whl", hash = "sha256:b6a85871a79d2e3b22d2d1b94ac2824226a63c6b741c88f7ae975f18b6778374"},
    {file = "iniconfig-2.0.0.tar.gz", hash = "sha256:2d91e135bf72d31a410b17c16da610a82cb55f6b0477d1a902134b24a455b8b3"},
]

[[package]]
name = "itsdangerous"
version = "2.2.0"
description = "Safely pass data to untrusted environments and back."
optional = false
python-versions = ">=3.8"
files = [
    {file = "itsdangerous-2.2.0-py3-none-any.whl", hash = "sha256:c6242fc49e35958c8b15141343aa660db5fc54d4f13a1db01a3f5891b98700ef"},
    {file = "itsdangerous-2.2.0.tar.gz", hash = "sha256:e0050c0b7da1eea53ffaf149c0cfbb5c6e2e2b69c4bef22c81fa6eb73e5f6173"},
]

[[package]]
name = "jinja2"
version = "3.1.4"
description = "A very fast and expressive template engine."
optional = false
python-versions = ">=3.7"
files = [
    {file = "jinja2-3.1.4-py3-none-any.whl", hash = "sha256:bc5dd2abb727a5319567b7a813e6a2e7318c39f4f487cfe6c89c6f9c7d25197d"},
    {file = "jinja2-3.1.4.tar.gz", hash = "sha256:4a3aee7acbbe7303aede8e9648d13b8bf88a429282aa6122a993f0ac800cb369"},
]

[package.dependencies]
MarkupSafe = ">=2.0"

[package.extras]
i18n = ["Babel (>=2.7)"]

[[package]]
name = "markupsafe"
version = "3.0.2"
description = "Safely add untrusted strings to HTML/XML markup."
optional = false
python-versions = ">=3.9"
files = [
    {file = "MarkupSafe-3.0.2-cp310-cp310-macosx_10_9_universal2.whl", hash = "sha256:7e94c425039cde14257288fd61dcfb01963e658efbc0ff54f5306b06054700f8"},
    {file = "MarkupSafe-3.0.2-cp310-cp310-macosx_11_0_arm64.whl", hash = "sha256:9e2d922824181480953426608b81967de705c3cef4d1af983af849d7bd619158"},
    {file = "MarkupSafe-3.0.2-cp310-cp310-manylinux_2_17_aarch64.manylinux2014_aarch64.whl", hash = "sha256:38a9ef736c01fccdd6600705b09dc574584b89bea478200c5fbf112a6b0d5579"},
    {file = "MarkupSafe-3.0.2-cp310-cp310-manylinux_2_17_x86_64.manylinux2014_x86_64.whl", hash = "sha256:bbcb445fa71794da8f178f0f6d66789a28d7319071af7a496d4d507ed566270d"},
    {file = "MarkupSafe-3.0.2-cp310-cp310-manylinux_2_5_i686.manylinux1_i686.manylinux_2_17_i686.manylinux2014_i686.whl", hash = "sha256:57cb5a3cf367aeb1d316576250f65edec5bb3be939e9247ae594b4bcbc317dfb"},
    {file = "MarkupSafe-3.0.2-cp310-cp310-musllinux_1_2_aarch64.whl", hash = "sha256:3809ede931876f5b2ec92eef964286840ed3540dadf803dd570c3b7e13141a3b"},
    {file = "MarkupSafe-3.0.2-cp310-cp310-musllinux_1_2_i686.whl", hash = "sha256:e07c3764494e3776c602c1e78e298937c3315ccc9043ead7e685b7f2b8d47b3c"},
    {file = "MarkupSafe-3.0.2-cp310-cp310-musllinux_1_2_x86_64.whl", hash = "sha256:b424c77b206d63d500bcb69fa55ed8d0e6a3774056bdc4839fc9298a7edca171"},
    {file = "MarkupSafe-3.0.2-cp310-cp310-win32.whl", hash = "sha256:fcabf5ff6eea076f859677f5f0b6b5c1a51e70a376b0579e0eadef8db48c6b50"},
    {file = "MarkupSafe-3.0.2-cp310-cp310-win_amd64.whl", hash = "sha256:6af100e168aa82a50e186c82875a5893c5597a0c1ccdb0d8b40240b1f28b969a"},
    {file = "MarkupSafe-3.0.2-cp311-cp311-macosx_10_9_universal2.whl", hash = "sha256:9025b4018f3a1314059769c7bf15441064b2207cb3f065e6ea1e7359cb46db9d"},
    {file = "MarkupSafe-3.0.2-cp311-cp311-macosx_11_0_arm64.whl", hash = "sha256:93335ca3812df2f366e80509ae119189886b0f3c2b81325d39efdb84a1e2ae93"},
    {file = "MarkupSafe-3.0.2-cp311-cp311-manylinux_2_17_aarch64.manylinux2014_aarch64.whl", hash = "sha256:2cb8438c3cbb25e220c2ab33bb226559e7afb3baec11c4f218ffa7308603c832"},
    {file = "MarkupSafe-3.0.2-cp311-cp311-manylinux_2_17_x86_64.manylinux2014_x86_64.whl", hash = "sha256:a123e330ef0853c6e822384873bef7507557d8e4a082961e1defa947aa59ba84"},
    {file = "MarkupSafe-3.0.2-cp311-cp311-manylinux_2_5_i686.manylinux1_i686.manylinux_2_17_i686.manylinux2014_i686.whl", hash = "sha256:1e084f686b92e5b83186b07e8a17fc09e38fff551f3602b249881fec658d3eca"},
    {file = "MarkupSafe-3.0.2-cp311-cp311-musllinux_1_2_aarch64.whl", hash = "sha256:d8213e09c917a951de9d09ecee036d5c7d36cb6cb7dbaece4c71a60d79fb9798"},
    {file = "MarkupSafe-3.0.2-cp311-cp311-musllinux_1_2_i686.whl", hash = "sha256:5b02fb34468b6aaa40dfc198d813a641e3a63b98c2b05a16b9f80b7ec314185e"},
    {file = "MarkupSafe-3.0.2-cp311-cp311-musllinux_1_2_x86_64.whl", hash = "sha256:0bff5e0ae4ef2e1ae4fdf2dfd5b76c75e5c2fa4132d05fc1b0dabcd20c7e28c4"},
    {file = "MarkupSafe-3.0.2-cp311-cp311-win32.whl", hash = "sha256:6c89876f41da747c8d3677a2b540fb32ef5715f97b66eeb0c6b66f5e3ef6f59d"},
    {file = "MarkupSafe-3.0.2-cp311-cp311-win_amd64.whl", hash = "sha256:70a87b411535ccad5ef2f1df5136506a10775d267e197e4cf531ced10537bd6b"},
    {file = "MarkupSafe-3.0.2-cp312-cp312-macosx_10_13_universal2.whl", hash = "sha256:9778bd8ab0a994ebf6f84c2b949e65736d5575320a17ae8984a77fab08db94cf"},
    {file = "MarkupSafe-3.0.2-cp312-cp312-macosx_11_0_arm64.whl", hash = "sha256:846ade7b71e3536c4e56b386c2a47adf5741d2d8b94ec9dc3e92e5e1ee1e2225"},
    {file = "MarkupSafe-3.0.2-cp312-cp312-manylinux_2_17_aarch64.manylinux2014_aarch64.whl", hash = "sha256:1c99d261bd2d5f6b59325c92c73df481e05e57f19837bdca8413b9eac4bd8028"},
    {file = "MarkupSafe-3.0.2-cp312-cp312-manylinux_2_17_x86_64.manylinux2014_x86_64.whl", hash = "sha256:e17c96c14e19278594aa4841ec148115f9c7615a47382ecb6b82bd8fea3ab0c8"},
    {file = "MarkupSafe-3.0.2-cp312-cp312-manylinux_2_5_i686.manylinux1_i686.manylinux_2_17_i686.manylinux2014_i686.whl", hash = "sha256:88416bd1e65dcea10bc7569faacb2c20ce071dd1f87539ca2ab364bf6231393c"},
    {file = "MarkupSafe-3.0.2-cp312-cp312-musllinux_1_2_aarch64.whl", hash = "sha256:2181e67807fc2fa785d0592dc2d6206c019b9502410671cc905d132a92866557"},
    {file = "MarkupSafe-3.0.2-cp312-cp312-musllinux_1_2_i686.whl", hash = "sha256:52305740fe773d09cffb16f8ed0427942901f00adedac82ec8b67752f58a1b22"},
    {file = "MarkupSafe-3.0.2-cp312-cp312-musllinux_1_2_x86_64.whl", hash = "sha256:ad10d3ded218f1039f11a75f8091880239651b52e9bb592ca27de44eed242a48"},
    {file = "MarkupSafe-3.0.2-cp312-cp312-win32.whl", hash = "sha256:0f4ca02bea9a23221c0182836703cbf8930c5e9454bacce27e767509fa286a30"},
    {file = "MarkupSafe-3.0.2-cp312-cp312-win_amd64.whl", hash = "sha256:8e06879fc22a25ca47312fbe7c8264eb0b662f6db27cb2d3bbbc74b1df4b9b87"},
    {file = "MarkupSafe-3.0.2-cp313-cp313-macosx_10_13_universal2.whl", hash = "sha256:ba9527cdd4c926ed0760bc301f6728ef34d841f405abf9d4f959c478421e4efd"},
    {file = "MarkupSafe-3.0.2-cp313-cp313-macosx_11_0_arm64.whl", hash = "sha256:f8b3d067f2e40fe93e1ccdd6b2e1d16c43140e76f02fb1319a05cf2b79d99430"},
    {file = "MarkupSafe-3.0.2-cp313-cp313-manylinux_2_17_aarch64.manylinux2014_aarch64.whl", hash = "sha256:569511d3b58c8791ab4c2e1285575265991e6d8f8700c7be0e88f86cb0672094"},
    {file = "MarkupSafe-3.0.2-cp313-cp313-manylinux_2_17_x86_64.manylinux2014_x86_64.whl", hash = "sha256:15ab75ef81add55874e7ab7055e9c397312385bd9ced94920f2802310c930396"},
    {file = "MarkupSafe-3.0.2-cp313-cp313-manylinux_2_5_i686.manylinux1_i686.manylinux_2_17_i686.manylinux2014_i686.whl", hash = "sha256:f3818cb119498c0678015754eba762e0d61e5b52d34c8b13d770f0719f7b1d79"},
    {file = "MarkupSafe-3.0.2-cp313-cp313-musllinux_1_2_aarch64.whl", hash = "sha256:cdb82a876c47801bb54a690c5ae105a46b392ac6099881cdfb9f6e95e4014c6a"},
    {file = "MarkupSafe-3.0.2-cp313-cp313-musllinux_1_2_i686.whl", hash = "sha256:cabc348d87e913db6ab4aa100f01b08f481097838bdddf7c7a84b7575b7309ca"},
    {file = "MarkupSafe-3.0.2-cp313-cp313-musllinux_1_2_x86_64.whl", hash = "sha256:444dcda765c8a838eaae23112db52f1efaf750daddb2d9ca300bcae1039adc5c"},
    {file = "MarkupSafe-3.0.2-cp313-cp313-win32.whl", hash = "sha256:bcf3e58998965654fdaff38e58584d8937aa3096ab5354d493c77d1fdd66d7a1"},
    {file = "MarkupSafe-3.0.2-cp313-cp313-win_amd64.whl", hash = "sha256:e6a2a455bd412959b57a172ce6328d2dd1f01cb2135efda2e4576e8a23fa3b0f"},
    {file = "MarkupSafe-3.0.2-cp313-cp313t-macosx_10_13_universal2.whl", hash = "sha256:b5a6b3ada725cea8a5e634536b1b01c30bcdcd7f9c6fff4151548d5bf6b3a36c"},
    {file = "MarkupSafe-3.0.2-cp313-cp313t-macosx_11_0_arm64.whl", hash = "sha256:a904af0a6162c73e3edcb969eeeb53a63ceeb5d8cf642fade7d39e7963a22ddb"},
    {file = "MarkupSafe-3.0.2-cp313-cp313t-manylinux_2_17_aarch64.manylinux2014_aarch64.whl", hash = "sha256:4aa4e5faecf353ed117801a068ebab7b7e09ffb6e1d5e412dc852e0da018126c"},
    {file = "MarkupSafe-3.0.2-cp313-cp313t-manylinux_2_17_x86_64.manylinux2014_x86_64.whl", hash = "sha256:c0ef13eaeee5b615fb07c9a7dadb38eac06a0608b41570d8ade51c56539e509d"},
    {file = "MarkupSafe-3.0.2-cp313-cp313t-manylinux_2_5_i686.manylinux1_i686.manylinux_2_17_i686.manylinux2014_i686.whl", hash = "sha256:d16a81a06776313e817c951135cf7340a3e91e8c1ff2fac444cfd75fffa04afe"},
    {file = "MarkupSafe-3.0.2-cp313-cp313t-musllinux_1_2_aarch64.whl", hash = "sha256:6381026f158fdb7c72a168278597a5e3a5222e83ea18f543112b2662a9b699c5"},
    {file = "MarkupSafe-3.0.2-cp313-cp313t-musllinux_1_2_i686.whl", hash = "sha256:3d79d162e7be8f996986c064d1c7c817f6df3a77fe3d6859f6f9e7be4b8c213a"},
    {file = "MarkupSafe-3.0.2-cp313-cp313t-musllinux_1_2_x86_64.whl", hash = "sha256:131a3c7689c85f5ad20f9f6fb1b866f402c445b220c19fe4308c0b147ccd2ad9"},
    {file = "MarkupSafe-3.0.2-cp313-cp313t-win32.whl", hash = "sha256:ba8062ed2cf21c07a9e295d5b8a2a5ce678b913b45fdf68c32d95d6c1291e0b6"},
    {file = "MarkupSafe-3.0.2-cp313-cp313t-win_amd64.whl", hash = "sha256:e444a31f8db13eb18ada366ab3cf45fd4b31e4db1236a4448f68778c1d1a5a2f"},
    {file = "MarkupSafe-3.0.2-cp39-cp39-macosx_10_9_universal2.whl", hash = "sha256:eaa0a10b7f72326f1372a713e73c3f739b524b3af41feb43e4921cb529f5929a"},
    {file = "MarkupSafe-3.0.2-cp39-cp39-macosx_11_0_arm64.whl", hash = "sha256:48032821bbdf20f5799ff537c7ac3d1fba0ba032cfc06194faffa8cda8b560ff"},
    {file = "MarkupSafe-3.0.2-cp39-cp39-manylinux_2_17_aarch64.manylinux2014_aarch64.whl", hash = "sha256:1a9d3f5f0901fdec14d8d2f66ef7d035f2157240a433441719ac9a3fba440b13"},
    {file = "MarkupSafe-3.0.2-cp39-cp39-manylinux_2_17_x86_64.manylinux2014_x86_64.whl", hash = "sha256:88b49a3b9ff31e19998750c38e030fc7bb937398b1f78cfa599aaef92d693144"},
    {file = "MarkupSafe-3.0.2-cp39-cp39-manylinux_2_5_i686.manylinux1_i686.manylinux_2_17_i686.manylinux2014_i686.whl", hash = "sha256:cfad01eed2c2e0c01fd0ecd2ef42c492f7f93902e39a42fc9ee1692961443a29"},
    {file = "MarkupSafe-3.0.2-cp39-cp39-musllinux_1_2_aarch64.whl", hash = "sha256:1225beacc926f536dc82e45f8a4d68502949dc67eea90eab715dea3a21c1b5f0"},
    {file = "MarkupSafe-3.0.2-cp39-cp39-musllinux_1_2_i686.whl", hash = "sha256:3169b1eefae027567d1ce6ee7cae382c57fe26e82775f460f0b2778beaad66c0"},
    {file = "MarkupSafe-3.0.2-cp39-cp39-musllinux_1_2_x86_64.whl", hash = "sha256:eb7972a85c54febfb25b5c4b4f3af4dcc731994c7da0d8a0b4a6eb0640e1d178"},
    {file = "MarkupSafe-3.0.2-cp39-cp39-win32.whl", hash = "sha256:8c4e8c3ce11e1f92f6536ff07154f9d49677ebaaafc32db9db4620bc11ed480f"},
    {file = "MarkupSafe-3.0.2-cp39-cp39-win_amd64.whl", hash = "sha256:6e296a513ca3d94054c2c881cc913116e90fd030ad1c656b3869762b754f5f8a"},
    {file = "markupsafe-3.0.2.tar.gz", hash = "sha256:ee55d3edf80167e48ea11a923c7386f4669df67d7994554387f84e7d8b0a2bf0"},
]

[[package]]
name = "packaging"
version = "24.1"
description = "Core utilities for Python packages"
optional = false
python-versions = ">=3.8"
files = [
    {file = "packaging-24.1-py3-none-any.whl", hash = "sha256:5b8f2217dbdbd2f7f384c41c628544e6d52f2d0f53c6d0c3ea61aa5d1d7ff124"},
    {file = "packaging-24.1.tar.gz", hash = "sha256:026ed72c8ed3fcce5bf8950572258698927fd1dbda10a5e981cdf0ac37f4f002"},
]

[[package]]
name = "pillow"
version = "11.0.0"
description = "Python Imaging Library (Fork)"
optional = false
python-versions = ">=3.9"
files = [
    {file = "pillow-11.0.0-cp310-cp310-macosx_10_10_x86_64.whl", hash = "sha256:6619654954dc4936fcff82db8eb6401d3159ec6be81e33c6000dfd76ae189947"},
    {file = "pillow-11.0.0-cp310-cp310-macosx_11_0_arm64.whl", hash = "sha256:b3c5ac4bed7519088103d9450a1107f76308ecf91d6dabc8a33a2fcfb18d0fba"},
    {file = "pillow-11.0.0-cp310-cp310-manylinux_2_17_aarch64.manylinux2014_aarch64.whl", hash = "sha256:a65149d8ada1055029fcb665452b2814fe7d7082fcb0c5bed6db851cb69b2086"},
    {file = "pillow-11.0.0-cp310-cp310-manylinux_2_17_x86_64.manylinux2014_x86_64.whl", hash = "sha256:88a58d8ac0cc0e7f3a014509f0455248a76629ca9b604eca7dc5927cc593c5e9"},
    {file = "pillow-11.0.0-cp310-cp310-manylinux_2_28_aarch64.whl", hash = "sha256:c26845094b1af3c91852745ae78e3ea47abf3dbcd1cf962f16b9a5fbe3ee8488"},
    {file = "pillow-11.0.0-cp310-cp310-manylinux_2_28_x86_64.whl", hash = "sha256:1a61b54f87ab5786b8479f81c4b11f4d61702830354520837f8cc791ebba0f5f"},
    {file = "pillow-11.0.0-cp310-cp310-musllinux_1_2_aarch64.whl", hash = "sha256:674629ff60030d144b7bca2b8330225a9b11c482ed408813924619c6f302fdbb"},
    {file = "pillow-11.0.0-cp310-cp310-musllinux_1_2_x86_64.whl", hash = "sha256:598b4e238f13276e0008299bd2482003f48158e2b11826862b1eb2ad7c768b97"},
    {file = "pillow-11.0.0-cp310-cp310-win32.whl", hash = "sha256:9a0f748eaa434a41fccf8e1ee7a3eed68af1b690e75328fd7a60af123c193b50"},
    {file = "pillow-11.0.0-cp310-cp310-win_amd64.whl", hash = "sha256:a5629742881bcbc1f42e840af185fd4d83a5edeb96475a575f4da50d6ede337c"},
    {file = "pillow-11.0.0-cp310-cp310-win_arm64.whl", hash = "sha256:ee217c198f2e41f184f3869f3e485557296d505b5195c513b2bfe0062dc537f1"},
    {file = "pillow-11.0.0-cp311-cp311-macosx_10_10_x86_64.whl", hash = "sha256:1c1d72714f429a521d8d2d018badc42414c3077eb187a59579f28e4270b4b0fc"},
    {file = "pillow-11.0.0-cp311-cp311-macosx_11_0_arm64.whl", hash = "sha256:499c3a1b0d6fc8213519e193796eb1a86a1be4b1877d678b30f83fd979811d1a"},
    {file = "pillow-11.0.0-cp311-cp311-manylinux_2_17_aarch64.manylinux2014_aarch64.whl", hash = "sha256:c8b2351c85d855293a299038e1f89db92a2f35e8d2f783489c6f0b2b5f3fe8a3"},
    {file = "pillow-11.0.0-cp311-cp311-manylinux_2_17_x86_64.manylinux2014_x86_64.whl", hash = "sha256:6f4dba50cfa56f910241eb7f883c20f1e7b1d8f7d91c750cd0b318bad443f4d5"},
    {file = "pillow-11.0.0-cp311-cp311-manylinux_2_28_aarch64.whl", hash = "sha256:5ddbfd761ee00c12ee1be86c9c0683ecf5bb14c9772ddbd782085779a63dd55b"},
    {file = "pillow-11.0.0-cp311-cp311-manylinux_2_28_x86_64.whl", hash = "sha256:45c566eb10b8967d71bf1ab8e4a525e5a93519e29ea071459ce517f6b903d7fa"},
    {file = "pillow-11.0.0-cp311-cp311-musllinux_1_2_aarch64.whl", hash = "sha256:b4fd7bd29610a83a8c9b564d457cf5bd92b4e11e79a4ee4716a63c959699b306"},
    {file = "pillow-11.0.0-cp311-cp311-musllinux_1_2_x86_64.whl", hash = "sha256:cb929ca942d0ec4fac404cbf520ee6cac37bf35be479b970c4ffadf2b6a1cad9"},
    {file = "pillow-11.0.0-cp311-cp311-win32.whl", hash = "sha256:006bcdd307cc47ba43e924099a038cbf9591062e6c50e570819743f5607404f5"},
    {file = "pillow-11.0.0-cp311-cp311-win_amd64.whl", hash = "sha256:52a2d8323a465f84faaba5236567d212c3668f2ab53e1c74c15583cf507a0291"},
    {file = "pillow-11.0.0-cp311-cp311-win_arm64.whl", hash = "sha256:16095692a253047fe3ec028e951fa4221a1f3ed3d80c397e83541a3037ff67c9"},
    {file = "pillow-11.0.0-cp312-cp312-macosx_10_13_x86_64.whl", hash = "sha256:d2c0a187a92a1cb5ef2c8ed5412dd8d4334272617f532d4ad4de31e0495bd923"},
    {file = "pillow-11.0.0-cp312-cp312-macosx_11_0_arm64.whl", hash = "sha256:084a07ef0821cfe4858fe86652fffac8e187b6ae677e9906e192aafcc1b69903"},
    {file = "pillow-11.0.0-cp312-cp312-manylinux_2_17_aarch64.manylinux2014_aarch64.whl", hash = "sha256:8069c5179902dcdce0be9bfc8235347fdbac249d23bd90514b7a47a72d9fecf4"},
    {file = "pillow-11.0.0-cp312-cp312-manylinux_2_17_x86_64.manylinux2014_x86_64.whl", hash = "sha256:f02541ef64077f22bf4924f225c0fd1248c168f86e4b7abdedd87d6ebaceab0f"},
    {file = "pillow-11.0.0-cp312-cp312-manylinux_2_28_aarch64.whl", hash = "sha256:fcb4621042ac4b7865c179bb972ed0da0218a076dc1820ffc48b1d74c1e37fe9"},
    {file = "pillow-11.0.0-cp312-cp312-manylinux_2_28_x86_64.whl", hash = "sha256:00177a63030d612148e659b55ba99527803288cea7c75fb05766ab7981a8c1b7"},
    {file = "pillow-11.0.0-cp312-cp312-musllinux_1_2_aarch64.whl", hash = "sha256:8853a3bf12afddfdf15f57c4b02d7ded92c7a75a5d7331d19f4f9572a89c17e6"},
    {file = "pillow-11.0.0-cp312-cp312-musllinux_1_2_x86_64.whl", hash = "sha256:3107c66e43bda25359d5ef446f59c497de2b5ed4c7fdba0894f8d6cf3822dafc"},
    {file = "pillow-11.0.0-cp312-cp312-win32.whl", hash = "sha256:86510e3f5eca0ab87429dd77fafc04693195eec7fd6a137c389c3eeb4cfb77c6"},
    {file = "pillow-11.0.0-cp312-cp312-win_amd64.whl", hash = "sha256:8ec4a89295cd6cd4d1058a5e6aec6bf51e0eaaf9714774e1bfac7cfc9051db47"},
    {file = "pillow-11.0.0-cp312-cp312-win_arm64.whl", hash = "sha256:27a7860107500d813fcd203b4ea19b04babe79448268403172782754870dac25"},
    {file = "pillow-11.0.0-cp313-cp313-macosx_10_13_x86_64.whl", hash = "sha256:bcd1fb5bb7b07f64c15618c89efcc2cfa3e95f0e3bcdbaf4642509de1942a699"},
    {file = "pillow-11.0.0-cp313-cp313-macosx_11_0_arm64.whl", hash = "sha256:0e038b0745997c7dcaae350d35859c9715c71e92ffb7e0f4a8e8a16732150f38"},
    {file = "pillow-11.0.0-cp313-cp313-manylinux_2_17_aarch64.manylinux2014_aarch64.whl", hash = "sha256:0ae08bd8ffc41aebf578c2af2f9d8749d91f448b3bfd41d7d9ff573d74f2a6b2"},
    {file = "pillow-11.0.0-cp313-cp313-manylinux_2_17_x86_64.manylinux2014_x86_64.whl", hash = "sha256:d69bfd8ec3219ae71bcde1f942b728903cad25fafe3100ba2258b973bd2bc1b2"},
    {file = "pillow-11.0.0-cp313-cp313-manylinux_2_28_aarch64.whl", hash = "sha256:61b887f9ddba63ddf62fd02a3ba7add935d053b6dd7d58998c630e6dbade8527"},
    {file = "pillow-11.0.0-cp313-cp313-manylinux_2_28_x86_64.whl", hash = "sha256:c6a660307ca9d4867caa8d9ca2c2658ab685de83792d1876274991adec7b93fa"},
    {file = "pillow-11.0.0-cp313-cp313-musllinux_1_2_aarch64.whl", hash = "sha256:73e3a0200cdda995c7e43dd47436c1548f87a30bb27fb871f352a22ab8dcf45f"},
    {file = "pillow-11.0.0-cp313-cp313-musllinux_1_2_x86_64.whl", hash = "sha256:fba162b8872d30fea8c52b258a542c5dfd7b235fb5cb352240c8d63b414013eb"},
    {file = "pillow-11.0.0-cp313-cp313-win32.whl", hash = "sha256:f1b82c27e89fffc6da125d5eb0ca6e68017faf5efc078128cfaa42cf5cb38798"},
    {file = "pillow-11.0.0-cp313-cp313-win_amd64.whl", hash = "sha256:8ba470552b48e5835f1d23ecb936bb7f71d206f9dfeee64245f30c3270b994de"},
    {file = "pillow-11.0.0-cp313-cp313-win_arm64.whl", hash = "sha256:846e193e103b41e984ac921b335df59195356ce3f71dcfd155aa79c603873b84"},
    {file = "pillow-11.0.0-cp313-cp313t-macosx_10_13_x86_64.whl", hash = "sha256:4ad70c4214f67d7466bea6a08061eba35c01b1b89eaa098040a35272a8efb22b"},
    {file = "pillow-11.0.0-cp313-cp313t-macosx_11_0_arm64.whl", hash = "sha256:6ec0d5af64f2e3d64a165f490d96368bb5dea8b8f9ad04487f9ab60dc4bb6003"},
    {file = "pillow-11.0.0-cp313-cp313t-manylinux_2_17_x86_64.manylinux2014_x86_64.whl", hash = "sha256:c809a70e43c7977c4a42aefd62f0131823ebf7dd73556fa5d5950f5b354087e2"},
    {file = "pillow-11.0.0-cp313-cp313t-manylinux_2_28_x86_64.whl", hash = "sha256:4b60c9520f7207aaf2e1d94de026682fc227806c6e1f55bba7606d1c94dd623a"},
    {file = "pillow-11.0.0-cp313-cp313t-musllinux_1_2_x86_64.whl", hash = "sha256:1e2688958a840c822279fda0086fec1fdab2f95bf2b717b66871c4ad9859d7e8"},
    {file = "pillow-11.0.0-cp313-cp313t-win32.whl", hash = "sha256:607bbe123c74e272e381a8d1957083a9463401f7bd01287f50521ecb05a313f8"},
    {file = "pillow-11.0.0-cp313-cp313t-win_amd64.whl", hash = "sha256:5c39ed17edea3bc69c743a8dd3e9853b7509625c2462532e62baa0732163a904"},
    {file = "pillow-11.0.0-cp313-cp313t-win_arm64.whl", hash = "sha256:75acbbeb05b86bc53cbe7b7e6fe00fbcf82ad7c684b3ad82e3d711da9ba287d3"},
    {file = "pillow-11.0.0-cp39-cp39-macosx_10_10_x86_64.whl", hash = "sha256:2e46773dc9f35a1dd28bd6981332fd7f27bec001a918a72a79b4133cf5291dba"},
    {file = "pillow-11.0.0-cp39-cp39-macosx_11_0_arm64.whl", hash = "sha256:2679d2258b7f1192b378e2893a8a0a0ca472234d4c2c0e6bdd3380e8dfa21b6a"},
    {file = "pillow-11.0.0-cp39-cp39-manylinux_2_17_aarch64.manylinux2014_aarch64.whl", hash = "sha256:eda2616eb2313cbb3eebbe51f19362eb434b18e3bb599466a1ffa76a033fb916"},
    {file = "pillow-11.0.0-cp39-cp39-manylinux_2_17_x86_64.manylinux2014_x86_64.whl", hash = "sha256:20ec184af98a121fb2da42642dea8a29ec80fc3efbaefb86d8fdd2606619045d"},
    {file = "pillow-11.0.0-cp39-cp39-manylinux_2_28_aarch64.whl", hash = "sha256:8594f42df584e5b4bb9281799698403f7af489fba84c34d53d1c4bfb71b7c4e7"},
    {file = "pillow-11.0.0-cp39-cp39-manylinux_2_28_x86_64.whl", hash = "sha256:c12b5ae868897c7338519c03049a806af85b9b8c237b7d675b8c5e089e4a618e"},
    {file = "pillow-11.0.0-cp39-cp39-musllinux_1_2_aarch64.whl", hash = "sha256:70fbbdacd1d271b77b7721fe3cdd2d537bbbd75d29e6300c672ec6bb38d9672f"},
    {file = "pillow-11.0.0-cp39-cp39-musllinux_1_2_x86_64.whl", hash = "sha256:5178952973e588b3f1360868847334e9e3bf49d19e169bbbdfaf8398002419ae"},
    {file = "pillow-11.0.0-cp39-cp39-win32.whl", hash = "sha256:8c676b587da5673d3c75bd67dd2a8cdfeb282ca38a30f37950511766b26858c4"},
    {file = "pillow-11.0.0-cp39-cp39-win_amd64.whl", hash = "sha256:94f3e1780abb45062287b4614a5bc0874519c86a777d4a7ad34978e86428b8dd"},
    {file = "pillow-11.0.0-cp39-cp39-win_arm64.whl", hash = "sha256:290f2cc809f9da7d6d622550bbf4c1e57518212da51b6a30fe8e0a270a5b78bd"},
    {file = "pillow-11.0.0-pp310-pypy310_pp73-macosx_10_15_x86_64.whl", hash = "sha256:1187739620f2b365de756ce086fdb3604573337cc28a0d3ac4a01ab6b2d2a6d2"},
    {file = "pillow-11.0.0-pp310-pypy310_pp73-macosx_11_0_arm64.whl", hash = "sha256:fbbcb7b57dc9c794843e3d1258c0fbf0f48656d46ffe9e09b63bbd6e8cd5d0a2"},
    {file = "pillow-11.0.0-pp310-pypy310_pp73-manylinux_2_17_aarch64.manylinux2014_aarch64.whl", hash = "sha256:5d203af30149ae339ad1b4f710d9844ed8796e97fda23ffbc4cc472968a47d0b"},
    {file = "pillow-11.0.0-pp310-pypy310_pp73-manylinux_2_17_x86_64.manylinux2014_x86_64.whl", hash = "sha256:21a0d3b115009ebb8ac3d2ebec5c2982cc693da935f4ab7bb5c8ebe2f47d36f2"},
    {file = "pillow-11.0.0-pp310-pypy310_pp73-manylinux_2_28_aarch64.whl", hash = "sha256:73853108f56df97baf2bb8b522f3578221e56f646ba345a372c78326710d3830"},
    {file = "pillow-11.0.0-pp310-pypy310_pp73-manylinux_2_28_x86_64.whl", hash = "sha256:e58876c91f97b0952eb766123bfef372792ab3f4e3e1f1a2267834c2ab131734"},
    {file = "pillow-11.0.0-pp310-pypy310_pp73-win_amd64.whl", hash = "sha256:224aaa38177597bb179f3ec87eeefcce8e4f85e608025e9cfac60de237ba6316"},
    {file = "pillow-11.0.0-pp39-pypy39_pp73-macosx_11_0_arm64.whl", hash = "sha256:5bd2d3bdb846d757055910f0a59792d33b555800813c3b39ada1829c372ccb06"},
    {file = "pillow-11.0.0-pp39-pypy39_pp73-manylinux_2_17_x86_64.manylinux2014_x86_64.whl", hash = "sha256:375b8dd15a1f5d2feafff536d47e22f69625c1aa92f12b339ec0b2ca40263273"},
    {file = "pillow-11.0.0-pp39-pypy39_pp73-manylinux_2_28_x86_64.whl", hash = "sha256:daffdf51ee5db69a82dd127eabecce20729e21f7a3680cf7cbb23f0829189790"},
 