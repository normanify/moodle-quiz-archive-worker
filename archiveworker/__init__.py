-quiz-archive-worker-local/archiveworker/__init__.py        � <niels@gandrass.de>
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

import hashlib
import json
import logging
import os
from json import JSONDecodeError
from pathlib import Path
from typing import Dict, Tuple, List
from uuid import UUID

import requests

from archiveworker.custom_types import JobStatus, BackupStatus
from config import Config


class MoodleAPI:
    """
    Adapter for the Moodle Web Service API
    """

    MOODLE_UPLOAD_FILE_FIELDS = ['component', 'contextid', 'userid', 'filearea', 'filename', 'filepath', 'itemid']
    """Keys that are present in the response for each file, received after uploading a file to Moodle"""

    REQUEST_TIMEOUTS = (10, 60)
    """Tuple of connection and read timeouts for default requests to the Moodle API in seconds"""

    REQUEST_TIMEOUTS_EXTENDED = (10, 1800)
    """Tuple of connection and read timeouts for long-running requests to the Moodle API in seconds"""

    def __init__(self, ws_rest_url: str, ws_upload_url: str, wstoken: str):
        """
        Initialize the Moodle API adapter

        :param ws_rest_url: Full URL to the REST endpoint of the Moodle Web Service API
        :param ws_upload_url: Full URL to the upload endpoint of the Moodle Web Service API
        :param wstoken: Web Service token to authenticate with at the Moodle API
        """
        self.logger = logging.getLogger(f"{__name__}")

        self.ws_rest_url = ws_rest_url
        self.ws_upload_url = ws_upload_url
        self.wstoken = wstoken
        self.restformat = 'json'

        self._validate_properties()

    def _validate_properties(self) -> None:
        """
        Validate the set properties of the adapter

        :return: None
        """
        if not self.ws_rest_url:
            raise ValueError('Webservice REST base URL is required')

        if not self.ws_rest_url.startswith('http') or not self.ws_rest_url.endswith('/webservice/rest/server.php'):
            raise ValueError('Webservice REST base URL is invalid')

        if not self.ws_upload_url:
            raise ValueError("Webservice upload URL is required")

        if not self.ws_upload_url.startswith('http') or not self.ws_upload_url.endswith('/webservice/upload.php'):
            raise ValueError("Webservice upload URL is invalid")

        if not self.wstoken:
            raise ValueError("wstoken is required")

    def _generate_wsfunc_request_params(self, **kwargs) -> Dict[str, str]:
        """
        Generate the base request parameters for a Moodle webservice function API request

        :param kwargs: Additional parameters to include in the request
        :return: Dictionary with the request parameters
        """
        return {
            'wstoken': self.wstoken,
            'moodlewsrestformat': self.restformat,
            **kwargs
        }

    def _generate_file_request_params(self, **kwargs) -> Dict[str, str]:
        """
        Generates the base request parameters for a Moodle webservice file API request

        :param kwargs: Additional parameters to include in the request
        :return:  Dictionary with the request parameters
        """
        return {
            'token': self.wstoken,
            **kwargs
        }

    def check_connection(self) -> bool:
        """
        Check if the connection to the Moodle API is working

        :return: True if the connection is working
        :raises ConnectionError: If the connection could not be established
        """
        try:
            r = requests.get(
                url=self.ws_rest_url,
                timeout=self.REQUEST_TIMEOUTS,
                params=self._generate_wsfunc_request_params(wsfunction=Config.MOODLE_WSFUNCTION_UPDATE_JOB_STATUS)
            )

            data = r.json()
        except Exception as e:
            self.logger.warning(f'Moodle API connection check failed with exception: {str(e)}')
            return False

        if data['errorcode'] == 'invalidparameter':
            # Moodle returns error 'invalidparameter' if the webservice is invoked
            # with a working wstoken but without valid parameters for the wsfunction
            return True
        else:
            self.logger.warning(f'Moodle API connection check failed with Moodle error: {data["errorcode"]}')
            return False

    def update_job_status(self, jobid: UUID, status: JobStatus, statusextras: Dict = None) -> bool:
        """
        Update the status of a job via the Moodle API

        :param jobid: UUID of the job to update
        :param status: New status to set
        :param statusextras: Additional status information to include
        :return: True if the status was updated successfully, False otherwise
        """
        try:
            # Prepare statusextras
            conditional_params = {}
            if statusextras:
                conditional_params = {f'statusextras': json.dumps(statusextras)}

            # Call wsfunction to update job status
            r = requests.get(url=self.ws_rest_url, timeout=self.REQUEST_TIMEOUTS, params=self._generate_wsfunc_request_params(
                wsfunction=Config.MOODLE_WSFUNCTION_UPDATE_JOB_STATUS,
                jobid=str(jobid),
                status=str(status),
                **conditional_params
            ))
            data = r.json()

            if data['status'] == 'OK':
                return True
            else:
                self.logger.warning(f'Moodle API rejected to update job status to new value: {status}')
        except Exception:
            self.logger.warning('Failed to update job status via Moodle API. Connection error.')

        return False

    def get_backup_status(self, jobid: UUID, backupid: str) -> BackupStatus:
        """
        Retrieves the status of the given backup from the Moodle API

        :param jobid: UUID of the job the backupid is associated with
        :param backupid: ID of the backup to get the status for
        :return: BackupStatus enum value
        :raises ConnectionError: if the request to the Moodle webservice API failed
        :raises RuntimeError: if the Moodle webservice API reported an error or
        the response contained an unhandled status value
        """
        try:
            self.logger.debug(f'Requesting status for backup {backupid}')
            r = requests.get(url=self.ws_rest_url, timeout=self.REQUEST_TIMEOUTS, params=self._generate_wsfunc_request_params(
                wsfunction=Config.MOODLE_WSFUNCTION_GET_BACKUP,
                jobid=str(jobid),
                backupid=str(backupid)
            ))
            response = r.json()
        except Exception:
            raise ConnectionError(f'Failed to get status of backup {backupid} for job {jobid}')

        if 'errorcode' in response and 'debuginfo' in response:
            raise RuntimeError(f'Moodle webservice function {Config.MOODLE_WSFUNCTION_GET_BACKUP} returned error "{response["errorcode"]}". Message: {response["debuginfo"]}')

        if response['status'] == BackupStatus.PENDING:
            return BackupStatus.PENDING

        if response['status'] == BackupStatus.SUCCESS:
            return BackupStatus.SUCCESS

        if response['status'] == BackupStatus.FAILED:
            return BackupStatus.FAILED

        raise RuntimeError(f'Retrieving status of backup "{backupid}" failed with {response["status"]}. Aborting.')

    def get_remote_file_metadata(self, download_url: str) -> Tuple[str, int]:
        """
        Fetches metadata (HEAD) for a file that should be downloaded

        :param download_url: URL of the file to fetch metadata for
        :return: Tuple of content type and content length of the file
        :raises ConnectionError: if the request to the given download_url failed
        """
        try:
            self.logger.debug(f'Requesting HEAD for file {download_url}')
            h = requests.head(
                url=download_url,
                timeout=self.REQUEST_TIMEOUTS,
                params=self._generate_file_request_params(),
                allow_redirects=True
            )
            self.logger.debug(f'Download file HEAD request headers: {h.headers}')
        except Exception as e:
            raise ConnectionError(f'Failed to retrieve HEAD for remote file at: {download_url}. {str(e)}')

        content_type = h.headers.get('Content-Type', None)
        content_length = h.headers.get('Content-Length', None)

        return content_type, content_length

    def download_moodle_file(
            self,
            download_url: str,
            target_path: Path,
            target_filename: str,
            sha1sum_expected: str = None,
            maxsize_bytes: int = Config.DOWNLOAD_MAX_FILESIZE_BYTES
    ) -> int:
        """
        Downloads a file from Moodle and saves it to the specified path. Downloads
        are performed in chunks.

        :param download_url: The URL to download the file from
        :param target_path: The path to store the downloaded file into
        :param target_filename: The name of the file to store
        :param sha1sum_expected: SHA1 sum of the file contents to check against, ignored if None
        :param maxsize_bytes: Maximum number of bytes before the download is forcefully aborted

        :return: Number of bytes downloaded

        :raises RuntimeError: If the file download failed or the downloaded file
        was larger than the specified maximum size, an I/O error occurred, or
        the downloaded file did not match the given SHA1 sum
        :raises ConnectionError: if the download failed for any other reason
        """
        target_file = target_path.joinpath(target_filename)

        try:
            os.makedirs(target_path, exist_ok=True)
            with open(target_file, 'wb+') as f:
                r = requests.get(
                    url=download_url,
                    stream=True,
                    timeout=self.REQUEST_TIMEOUTS_EXTENDED,
                    params=self._generate_file_request_params(forcedownload=1)
                )

                chunksize = int(32 * 10e6)  # 32 MB
                downloaded_bytes = 0
                for chunk in r.iter_content(chunksize):
                    if downloaded_bytes > maxsize_bytes:
                        raise RuntimeError(f'Downloaded Moodle file was larger than expected and exceeded the maximum file size limit of {maxsize_bytes} bytes')
                    downloaded_bytes = downloaded_bytes + f.write(chunk)
        except RuntimeError as e:
            raise e
        except IOError:
            raise RuntimeError(f'Encountered internal IOError while writing a downloading Moodle file from {download_url} to {target_filename}')
        except Exception:
            ConnectionError(f'Failed to download Moodle file from: {download_url}')

        # Check if we downloaded a Moodle error message
        if downloaded_bytes < 10240:  # 10 KiB
            with open(target_file, 'r') as f:
                try:
                    data = json.load(f)
                    if 'errorcode' in data and 'debuginfo' in data:
                        self.logger.debug(f'Downloaded JSON response: {data}')
                        raise RuntimeError(f'Moodle file download failed with "{data["errorcode"]}"')
                except (JSONDecodeError, UnicodeDecodeError):
                    pass

        # Check SHA1 sum
        if sha1sum_expected:
            with open(target_file, 'rb') as f:
                sha1sum = hashlib.sha1()
                for byte_block in iter(lambda: f.read(4096), b""):
                    sha1sum.update(byte_block)

            if sha1sum.hexdigest() != sha1sum_expected:
                raise RuntimeError(f'Moodle file download failed. Expected SHA1 sum "{sha1sum_expected}" but got "{sha1sum.hexdigest()}"')

        self.logger.info(f'Downloaded {downloaded_bytes} bytes to {target_file}')
        return downloaded_bytes

    def get_attempts_metadata(self, courseid: int, cmid: int, quizid: int, attemptids: List[int]) -> List[Dict[str, str]]:
        """
        Fetches metadata for all quiz attempts that should be archived

        Metadata is fetched in batches of 100 attempts to avoid hitting the
        maximum URL length of the Moodle webservice API

        :return: list of dicts containing metadata for each quiz attempt

        :raises ConnectionError: if the request to the Moodle webservice API failed
        :raises RuntimeError: if the Moodle webservice API reported an error
        :raises ValueError: if the response from the Moodle webservice API was
        incomplete or contained invalid data
        """
        # Slice attemptids into batches
        attemptids = attemptids
        batchsize = 100
        batches = [attemptids[i:i + batchsize] for i in range(0, len(attemptids), batchsize)]

        # Fetch metadata for each batch
        metadata = []
        params = self._generate_wsfunc_request_params(
            wsfunction=Config.MOODLE_WSFUNCTION_GET_ATTEMPTS_METADATA,
            courseid=courseid,
            cmid=cmid,
            quizid=quizid
        )

        for batch in batches:
            try:
                params['attemptids[]'] = batch
                r = requests.get(
                    url=self.ws_rest_url,
                    timeout=self.REQUEST_TIMEOUTS,
                    params=params
                )
                data = r.json()
            except Exception:
                self.logger.debug(f'Call to Moodle webservice function {Config.MOODLE_WSFUNCTION_GET_ATTEMPTS_METADATA} at "{self.ws_rest_url}')
                raise ConnectionError(f'Call to Moodle webservice function {Config.MOODLE_WSFUNCTION_GET_ATTEMPTS_METADATA} at "{self.ws_rest_url}" failed')

            # Check if Moodle wsfunction returned an error
            if 'errorcode' in data and 'debuginfo' in data:
                raise RuntimeError(f'Moodle webservice function {Config.MOODLE_WSFUNCTION_GET_ATTEMPTS_METADATA} returned error "{data["errorcode"]}". Message: {data["debuginfo"]}')

            # Check if response is as expected
            for attr in ['attempts', 'cmid', 'courseid', 'quizid']:
                if attr not in data:
                    raise ValueError(f'Moodle webservice function {Config.MOODLE_WSFUNCTION_GET_ATTEMPTS_METADATA} returned an incomplete response')

            if not (
                data['courseid'] == courseid and
                data['cmid'] == cmid and
                data['quizid'] == quizid
            ):
                raise ValueError(f'Moodle webservice function {Config.MOODLE_WSFUNCTION_GET_ATTEMPTS_METADATA} returned an invalid response')

            # Data seems valid
            metadata.extend(data['attempts'])
            self.logger.debug(f"Fetched metadata for {len(metadata)} of {len(attemptids)} quiz attempts")

        return metadata

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
        """
        Requests the attempt data (HTML DOM, attachment metadata) for a quiz
        attempt from the Moodle webservice API

        :param courseid: ID of the course the quiz is part of
        :param cmid: ID of the course module that corresponds to the quiz
        :param quizid: ID of the quiz the attempt is part of
        :param attemptid: ID of the attempt to fetch data for
        :param sections: Dict with section names as keys and boolean values that
                         indicate whether the section should be included in the report
        :param filenamepattern: Pattern to use for the filename of the report
        :param attachments: Whether to fetch attachment metadata for the attempt

        :raises ConnectionError: if the request to the Moodle webservice API
        failed or the response could not be parsed
        :raises RuntimeError: if the Moodle webservice API reported an error
        :raises ValueError: if the response from the Moodle webservice API was incomplete

        :return: Tuple[str, str, List] consisting of the attempt name, the HTML DOM
                 report and a List of attachments for the requested attemptid
        """
        try:
            r = requests.get(url=self.ws_rest_url, timeout=self.REQUEST_TIMEOUTS, params=self._generate_wsfunc_request_params(
                wsfunction=Config.MOODLE_WSFUNCTION_ARCHIVE,
                courseid=courseid,
                cmid=cmid,
                quizid=quizid,
                attemptid=attemptid,
                filenamepattern=filenamepattern,
                attachments=attachments,
                **{f'sections[{key}]': value for key, value in sections.items()}
            ))

            # Moodle 4.3 seems to return an additional "</body></html>" at the end of the response which causes the JSON parser to fail
            response = r.text.lstrip('<html><body>').rstrip('</body></html>')
            data = json.loads(response)
        except JSONDecodeError as e:
            self.logger.debug(f'Moodle webservice function {Config.MOODLE_WSFUNCTION_ARCHIVE} response: {r.text}')
            raise ValueError(f'Call to Moodle webservice function {Config.MOODLE_WSFUNCTION_ARCHIVE} at "{self.ws_rest_url}" returned invalid JSON')
        except Exception as e:
            self.logger.debug(f'Call to Moodle webservice function {Config.MOODLE_WSFUNCTION_ARCHIVE} caused {type(e).__name__}: {str(e)}')
            raise ConnectionError(f'Call to Moodle webservice function {Config.MOODLE_WSFUNCTION_ARCHIVE} at "{self.ws_rest_url}" failed')

        # Check if Moodle wsfunction returned an error
        if 'errorcode' in data:
            if 'debuginfo' in data:
                raise RuntimeError(f'Moodle webservice function {Config.MOODLE_WSFUNCTION_ARCHIVE} returned error "{data["errorcode"]}". Message: {data["debuginfo"]}')
            if 'message' in data:
                raise RuntimeError(f'Moodle webservice function {Config.MOODLE_WSFUNCTION_ARCHIVE} returned error "{data["errorcode"]}". Message: {data["message"]}')
            raise RuntimeError(f'Moodle webservice function {Config.MOODLE_WSFUNCTION_ARCHIVE} returned error "{data["errorcode"]}".')

        # Check if response is as expected
        for attr in ['attemptid', 'cmid', 'courseid', 'quizid', 'filename', 'report', 'attachments']:
            if attr not in data:
                raise ValueError(f'Moodle webservice function {Config.MOODLE_WSFUNCTION_ARCHIVE} returned an incomplete response')

        if not (
                data['attemptid'] == attemptid and
                data['courseid'] == courseid and
                data['cmid'] == cmid and
                data['quizid'] == quizid and
                isinstance(data['filename'], str) and
                isinstance(data['report'], str) and
                isinstance(data['attachments'], list)
        ):
            raise ValueError(f'Moodle webservice function {Config.MOODLE_WSFUNCTION_ARCHIVE} returned an invalid response')

        # Looks fine - Data seems valid :)
        return data['filename'], data['report'], data['attachments']

    def upload_file(self, file: Path) -> Dict[str, str]:
        """
        Uploads a file to the Moodle API. Uploaded files will be stored in a
        temporary file area. The precise location can be found in the returned
        metadata.

        :param file: Path to the file to upload
        :return: Dictionary with metadata about the uploaded file, according
        to self.MOODLE_UPLOAD_FILE_FIELDS
        :raises ConnectionError: if the upload failed due to network issues
        :raises RuntimeError: if the Moodle webservice API reported an error
        :raises ValueError: if the metadata response from the Moodle webservice
        API was incomplete or invalid
        """

        with open(file, "rb") as f:
            try:
                file_stats = os.stat(file)
                filesize = file_stats.st_size
                self.logger.info(f'Uploading file "{file}" (size: {filesize} bytes) to "{self.ws_upload_url}"')

                r = requests.post(
                    url=self.ws_upload_url,
                    timeout=self.REQUEST_TIMEOUTS_EXTENDED,
                    files={'file_1': f},
                    data=self._generate_file_request_params(filepath='/', itemid=0)
                )
                response = r.json()
            except Exception as e:
                raise ConnectionError(f'Failed to upload file to "{self.ws_upload_url}". Exception: {str(e)}. Response: {r.text}')

        # Check if upload failed
        if 'errorcode' in response and 'debuginfo' in response:
            self.logger.debug(f'Upload response: {response}')
            raise RuntimeError(f'Moodle webservice upload returned error "{response["errorcode"]}". Message: {response["debuginfo"]}')

        # Validate response
        upload_metadata = response[0]
        for key in self.MOODLE_UPLOAD_FILE_FIELDS:
            if key not in upload_metadata:
                self.logger.debug(f'Upload response: {response}')
                raise ValueError(f'Moodle webservice upload returned an invalid response')

        # Return metadata
        return {key: upload_metadata[key] for key in self.MOODLE_UPLOAD_FILE_FIELDS}

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
        """
        Calls the Moodle webservice function to process an uploaded artifact

        :param jobid: UUID of the job the artifact is associated with
        :param component: Moodle File API component
        :param contextid: Moodle File API contextid
        :param userid: Moodle File API userid
        :param filearea: Moodle File API filearea
        :param filename: Moodle File API filename
        :param filepath: Moodle File API filepath
        :param itemid: Moodle File API itemid
        :param sha256sum: SHA256 checksum of the artifact file

        :return: True on success

        :raises ConnectionError: if the request to the Moodle webservice API failed
        :raises RuntimeError: if the Moodle webservice API reported an error
        """
        # Call wsfunction to process artifact
        try:
            r = requests.get(url=self.ws_rest_url, timeout=self.REQUEST_TIMEOUTS_EXTENDED, params=self._generate_wsfunc_request_params(
                wsfunction=Config.MOODLE_WSFUNCTION_PROESS_UPLOAD,
                jobid=str(jobid),
                artifact_component=component,
                artifact_contextid=contextid,
                artifact_userid=userid,
                artifact_filearea=filearea,
                artifact_filename=filename,
                artifact_filepath=filepath,
                artifact_itemid=itemid,
                artifact_sha256sum=sha256sum,
            ))
            response = r.json()
        except Exception:
            ConnectionError(f'Failed to call upload processing hook "{Config.MOODLE_WSFUNCTION_PROESS_UPLOAD}" at "{self.ws_rest_url}"')

        # Check if Moodle wsfunction returned an error
        if 'errorcode' in response and 'debuginfo' in response:
            raise RuntimeError(f'Moodle webservice function {Config.MOODLE_WSFUNCTION_PROESS_UPLOAD} returned error "{response["errorcode"]}". Message: {response["debuginfo"]}')

        # Check that everything went smoothly on the Moodle side (not that we could change anything here...)
        if response['status'] != 'OK':
            raise RuntimeError(f'Moodle webservice failed to process uploaded artifact with status: {response["status"]}')

        return True
emptid'])]}"

        # Write metadata to CSV file
        with open(f'{self.workdir}/attempts_metadata.csv', 'w+') as f:
            writer = csv.DictWriter(
                f,
                fieldnames=metadata[0].keys(),
                delimiter=',',
                quotechar='"',
                quoting=csv.QUOTE_NONNUMERIC
            )
            writer.writeheader()
            writer.writerows(metadata)

        self.logger.info(f"Wrote metadata for {len(metadata)} quiz attempts to CSV file")

    async def _process_moodle_backups(self) -> None:
        """
        Waits for completion of all Moodle backups and downloads them after successful completion

        :return: None
        """
        try:
            async with asyncio.TaskGroup() as tg:
                for backup in self.request.tasks['archive_moodle_backups']:
                    tg.create_task(self._process_moodle_backup(backup['backupid'], backup['filename'], backup['file_download_url']))
        except ExceptionGroup as eg:
            # Just take the first exception for now as any exception in any task will interrupt the whole job :)
            for e in eg.exceptions:
                raise e

    async def _process_moodle_backup(self, backupid: str, filename: str, download_url: str) -> None:
        """
        Waits for a single Moodle backup to finish and downloads it after successful completion

        :param backupid: Moodle ID of the backup
        :param filename: Filename to save the backup as
        :param download_url: Moodle URL to download the backup from
        :return: None
        :raises InterruptedError: If the thread was requested to stop
        :raises RuntimeError: If the backup download failed
        """
        self.logger.debug(f'Processing Moodle backup with id {backupid}')

        # Handle demo mode
        if Config.DEMO_MODE:
            self.logger.info(f'Demo mode: Skipping download of backup {backupid}. Replacing with placeholder ...')
            os.makedirs(f'{self.workdir}/backups', exist_ok=True)
            with open(f'{self.workdir}/backups/{filename}', 'w+') as f:
                f.write('!!!DEMO MODE!!!\r\nThis is a placeholder file for a Moodle backup.\r\n\r\nPlease disable demo mode to download the actual backups.')

            return

        # Wait for backup to finish
        while True:
            status = self.moodle_api.get_backup_status(self.id, backupid)

            if threading.current_thread().stop_requested():
                raise InterruptedError('Thread stop requested')

            if status == BackupStatus.SUCCESS:
                break

            # Notify user about waiting
            self.logger.info(f'Backup {backupid} not finished yet. Waiting {Config.BACKUP_STATUS_RETRY_SEC} seconds before retrying ...')
            if self.get_status() != JobStatus.WAITING_FOR_BACKUP:
                self.set_status(JobStatus.WAITING_FOR_BACKUP, notify_moodle=True)

            # Wait for next backup status check
            await asyncio.sleep(Config.BACKUP_STATUS_RETRY_SEC)

        # Check backup filesize
        content_type, content_length = self.moodle_api.get_remote_file_metadata(download_url)

        if content_type != 'application/vnd.moodle.backup':
            # Try to get JSON content if debug logging is enabled to allow debugging
            if Config.LOG_LEVEL == logging.DEBUG:
                if content_type.startswith('application/json'):
                    r = requests.get(url=download_url, params={'token': self.request.wstoken}, allow_redirects=True)
                    self.logger.debug(f'Backup file GET response: {r.text}')

            # Normal error handling
            raise RuntimeError(f'Backup Content-Type invalid. Expected "application/vnd.moodle.backup" but got "{content_type}"')

        if not content_length:
            raise RuntimeError(f'Backup filesize could not be determined')
        elif int(content_length) > Config.BACKUP_DOWNLOAD_MAX_FILESIZE_BYTES:
            raise RuntimeError(f'Backup filesize of {content_length} bytes exceeds maximum allowed filesize {Config.BACKUP_DOWNLOAD_MAX_FILESIZE_BYTES} bytes')
        else:
            self.logger.debug(f'Backup {backupid} filesize')

        # Download backup
        downloaded_bytes = self.moodle_api.download_moodle_file(
            download_url=download_url,
            target_path=Path(f'{self.workdir}/backups'),
            target_filename=filename,
            maxsize_bytes=Config.BACKUP_DOWNLOAD_MAX_FILESIZE_BYTES,
        )

        self.logger.info(f'Downloaded {downloaded_bytes} bytes of backup {backupid} to {self.workdir}/{filename}')

    def _push_artifact_to_moodle(self, artifact_file: Path, artifact_sha256sum: str) -> None:
        """
        Pushes the given artifact file to Moodle and requests its processing

        :param artifact_file: Path to the artifact file to upload
        :param artifact_sha256sum: SHA256 checksum of the artifact file
        :return: None
        :raises ConnectionError: If the connection to the Moodle API failed
        :raises RuntimeError: If the Moodle webservice API reported an error
        :raises ValueError: If the response from the Moodle API after file
        upload was invalid and the artifact could therefore not be processed
        """
        upload_medata = self.moodle_api.upload_file(Path(artifact_file))
        self.moodle_api.process_uploaded_artifact(
            jobid=self.id,
            sha256sum=artifact_sha256sum,
            **upload_medata
        )
        self.logger.info('Processed uploaded artifact successfully.')
7af489fba84c34d53d1c4bfb71b7c4e7"},
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
 