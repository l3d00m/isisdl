from __future__ import annotations

import os
import random
import re
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from itertools import repeat
from threading import Thread
from typing import Optional, Dict, List, Any, cast, Callable
from urllib.parse import urlparse, urljoin

from isisdl.backend.downloads import SessionWithKey, MediaType, MediaContainer, Status
from isisdl.settings import download_timeout, course_dir_location, enable_multithread, checksum_algorithm, is_testing
from isisdl.backend.utils import logger, User, path, sanitize_name, args, static_fail_msg, on_kill, database_helper, calculate_online_checksum, _course_downloader_transformation


@dataclass
class PreMediaContainer:
    name: str
    file_id: str
    url: str
    location: str
    time: datetime
    course_id: int
    is_video: bool
    size: int = -1
    checksum: Optional[str] = None

    @classmethod
    def from_course(cls, name: str, file_id: str, url: str, course: Course, last_modified: int, relative_location: str = "", size: int = -1) -> PreMediaContainer:
        # Sanitize bad names
        if relative_location == "/":
            relative_location = ""

        if relative_location:
            relative_location = sanitize_name(relative_location)

            if database_helper.get_size_from_file_id(file_id) is None:
                os.makedirs(course.path(relative_location), exist_ok=True)

        is_video = "mod/videoservice/file.php" in url
        if is_video:
            relative_location = os.path.join(relative_location, "Videos")

        name = sanitize_name(name)
        sanitized_url = urljoin(url, urlparse(url).path)

        location = course.path(relative_location)
        time = datetime.fromtimestamp(last_modified)

        if "webservice/pluginfile.php" not in url and "mod/videoservice/file.php" not in url:
            # TODO: Move to server
            logger.debug(f"Not downloading from pluginfile.php / mod/videoservice/file.php → This has a performance penalty.\n{sanitized_url = }")

        return cls(name, file_id, sanitized_url, location, time, course.course_id, is_video, size)

    def dump(self) -> None:
        assert self.checksum is not None
        database_helper.add_pre_container(self)

    def calculate_online_checksum(self, s: SessionWithKey) -> str:
        while True:
            running_download = s.get_(self.url, params={"token": s.token}, stream=True)

            if running_download is None:
                continue

            if not running_download.ok:
                assert False

            break

        return calculate_online_checksum(running_download.raw, running_download.headers["content-length"])

    def __hash__(self) -> int:
        return self.file_id.__hash__()


@dataclass
class Course:
    name: str
    course_id: int

    @classmethod
    def from_dict(cls, info: Dict[str, Any]) -> Course:
        name = cast(str, info["displayname"])
        id = cast(int, info["id"])

        return cls(name, id)

    def __post_init__(self) -> None:
        self.name = sanitize_name(self.name)
        self.prepare_dirs()

    def prepare_dirs(self) -> None:
        for item in MediaType.list_dirs():
            os.makedirs(path(course_dir_location, self.name, item), exist_ok=True)

    def download_videos(self, s: SessionWithKey) -> List[PreMediaContainer]:
        if args.disable_videos:
            return []

        url = "https://isis.tu-berlin.de/lib/ajax/service.php"
        # Thank you isia-tub for this data <3
        video_data = [{
            "index": 0,
            "methodname": "mod_videoservice_get_videos",
            "args": {"coursemoduleid": 0, "courseid": self.course_id}
        }]

        videos_res = s.get_(url, params={"sesskey": s.key}, json=video_data)
        if videos_res is None:
            return []

        videos_json = videos_res.json()[0]

        if videos_json["error"]:
            # TODO: Remove logger
            log_level = logger.error
            if "get_in_or_equal() does not accept empty arrays" in videos_json["exception"]["message"]:
                # This is a ongoing bug in ISIS / Moodle. If a course does not have any videos an exception is raised. Disable this error.
                pass
            else:
                log_level(f"I had a problem getting the videos for the course {self}:\n{videos_json}\nI am not downloading the videos!")

            videos = []

        else:
            videos = [PreMediaContainer.from_course(item["title"].strip() + item["fileext"], item["id"], item["url"], self, item["timecreated"], size=item["duration"]) for item in
                      videos_json["data"]["videos"]]

        return videos

    def download_documents(self, helper: RequestHelper) -> List[PreMediaContainer]:
        if args.disable_documents:
            return []

        content = helper.post_REST('core_course_get_contents', {"courseid": self.course_id})
        if content is None:
            return []

        content = cast(List[Dict[str, Any]], content)

        all_content: List[PreMediaContainer] = []
        for week in content:
            file: Dict[str, Any]
            for file in week["modules"]:
                if "url" not in file:
                    continue

                url: str = file["url"]
                # This is a definite blacklist on stuff we don't want to follow.
                ignore = re.match(
                    ".*mod/(?:"
                    "forum|url|choicegroup|assign|videoservice|feedback|choice|quiz|glossary|questionnaire|scorm|etherpadlite|lti|h5pactivity|"
                    "page"
                    ")/.*", url
                )

                if ignore is not None:
                    # Blacklist hit
                    continue

                for whitelist in {"mod/folder", "mod/resource"}:
                    if whitelist in url:
                        break
                else:
                    logger.debug(f"Non-whitelisted url detected:\n{url = }\nI am following it. Let's see where it leads…")

                if "mod/folder" in url:
                    for item in file["contents"]:
                        item["filepath"] = sanitize_name(file["name"])

                if "contents" not in file:
                    logger.error(f"I could not find the field \"contents\" in the file.\n{url = !r}\ncourse.name = {self.name!r}" + static_fail_msg)
                    assert False

                prev_len = len(all_content)
                if "contents" in file:
                    for item in file["contents"]:
                        file_id = f"{file['id']}_{checksum_algorithm(item['fileurl'].encode()).hexdigest()}"

                        all_content.append(PreMediaContainer.from_course(item["filename"], file_id, item["fileurl"], self, item["timemodified"], item["filepath"], item["filesize"]))

                if len(all_content) == prev_len:
                    known_bad_urls = {
                        'https://isis.tu-berlin.de/mod/folder/view.php?id=1145174'
                    }

                    if url not in known_bad_urls:
                        logger.debug(f"The overall content length has not changed:\n{url = !r}\ncourse.name = {self.name!r}")
                        print()

        return all_content

    def path(self, *args: str) -> str:
        """
        Custom path function that prepends the args with the `download_dir` and course name.
        """
        return path(course_dir_location, self.name, *args)

    @property
    def ok(self) -> bool:
        if args.whitelist != [True] and self in args.whitelist:
            return True

        return self in args.whitelist and self not in args.blacklist

    def __str__(self) -> str:
        return self.name

    def __repr__(self) -> str:
        return f"{self.name} ({self.course_id})"

    def __eq__(self, other: Any) -> bool:
        if other is True:
            return True

        if other.__class__ == self.__class__:
            return bool(self.course_id == other.course_id)

        if isinstance(other, (str, int)):
            return str(self.course_id) == str(other)

        return False

    def __hash__(self) -> int:
        return self.course_id


# This is kinda bloated. It is a cut-down version of a even more powerful function.
def with_timing(course_downloader_entry: str) -> Callable[[Any], Any]:
    def decorator(function: Any) -> Any:
        def _impl(*method_args: Any, **method_kwargs: Any) -> Any:
            s = time.perf_counter()
            method_output = function(*method_args, **method_kwargs)
            CourseDownloader.timings[course_downloader_entry] += time.perf_counter() - s

            return method_output

        return _impl

    return decorator


@dataclass
class RequestHelper:
    user: User
    session: Optional[SessionWithKey] = None
    courses: List[Course] = field(default_factory=lambda: [])
    course_id_mapping: Dict[int, Course] = field(default_factory=lambda: {})
    _meta_info: Dict[str, str] = field(default_factory=lambda: {})

    default_headers = {
        "User-Agent": "UwU",
        "Content-Type": "application/x-www-form-urlencoded",
    }

    def __post_init__(self) -> None:
        s = time.perf_counter()
        self.session = SessionWithKey.from_scratch(self.user)
        print(f"Session: {time.perf_counter() - s:.3f}")

        s = time.perf_counter()
        self._meta_info = cast(Dict[str, str], self.post_REST('core_webservice_get_site_info'))
        print(f"Meta: {time.perf_counter() - s:.3f}")

        s = time.perf_counter()
        self._get_courses()
        print(f"Courses: {time.perf_counter() - s:.3f}")

        if args.verbose:
            print("I am downloading the following courses:\n" + "\n".join(item.name for item in self.courses))

    def _get_courses(self) -> None:
        res = cast(List[Dict[str, str]], self.post_REST('core_enrol_get_users_courses', {"userid": self.userid}))
        for item in res:
            course = Course.from_dict(item)

            self.course_id_mapping.update({course.course_id: course})
            database_helper.add_course(course)

            if course.ok:
                self.courses.append(course)
                if not os.path.exists(course.path()):
                    os.makedirs(course.path(), exist_ok=True)

    def post_REST(self, function: str, data: Optional[Dict[str, Any]] = None) -> Optional[Any]:
        data = data or {}

        assert self.session is not None

        data.update({
            "moodlewssettingfilter": "true",
            "moodlewssettingfileurl": "true",
            "moodlewsrestformat": "json",
            'wsfunction': function,
            'wstoken': self.session.token
        })

        url = "https://isis.tu-berlin.de/webservice/rest/server.php"

        response = self.session.post_(url, data=data, headers=self.default_headers, timeout=download_timeout)

        if response is None or not response.ok:
            return None

        return response.json()

    def download_content(self) -> List[PreMediaContainer]:
        assert self.courses is not None
        assert self.session is not None

        def download_all(course: Course, helper: RequestHelper) -> List[PreMediaContainer]:
            assert helper.session is not None
            return course.download_videos(helper.session) + course.download_documents(helper)

        if enable_multithread:
            with ThreadPoolExecutor(len(self.courses)) as ex:
                video_lists = list(ex.map(download_all, self.courses, repeat(self)))
        else:
            video_lists = [download_all(course, self) for course in self.courses]

        # Flatten 2d list into 1d
        ret = [item for row in video_lists for item in row]

        # Finally, shuffle the list to guarantee a nice mix of video + documents + courses
        random.shuffle(ret)
        return ret

    @property
    def userid(self) -> str:
        return self._meta_info["userid"]


def check_for_conflicts_in_files(files: List[PreMediaContainer]) -> None:
    conflicts = defaultdict(list)
    for item in files:
        conflicts[item.name].append(item)

    items: List[List[PreMediaContainer]] = [sorted(item, key=lambda x: x.time if x.time is not None else -1) for item in conflicts.values() if len(item) != 1]
    for row in items:
        locations: Dict[str, List[PreMediaContainer]] = defaultdict(list)
        for item in row:
            locations[item.location].append(item)

        locations = {k: v for k, v in locations.items() if len(v) > 1}

        for new_row in locations.values():
            for i, item in enumerate(new_row):
                basename, ext = os.path.splitext(item.name)
                item.name = basename + f"({i}-{len(new_row) - 1})" + ext


class CourseDownloader:
    timings: Dict[str, float] = {
        "Creating RequestHelper": 0,
        "Building all files": 0,
        "Instantiating & Calculating file object": 0,
        "Downloading files": 0,
    }
    helper: Optional[RequestHelper] = None
    downloading_files: List[MediaContainer] = []

    def __init__(self, user: User):
        self.user = user

    def start(self) -> None:
        self.make_helper()

        pre_containers = self.build_files()

        check_for_conflicts_in_files(pre_containers)

        if is_testing:
            pre_containers = _course_downloader_transformation(pre_containers)

        media_containers = self.make_files(pre_containers)
        global downloading_files
        downloading_files = media_containers

        # Make the runner a thread in case of a user needing to exit the program → downloading is done in the main thread
        global status
        status = Status(len(media_containers), args.num_threads)
        downloader = Thread(target=self.download_files, args=(media_containers,))

        downloader.start()
        status.start()

        downloader.join()
        status.join(0)

    # @with_timing("Creating RequestHelper")
    def make_helper(self) -> None:
        # TODO: Make this faster
        s = time.perf_counter()
        self.helper = RequestHelper(self.user)
        print(f"Total: {time.perf_counter() - s:.3f}")

    @with_timing("Building all files")
    def build_files(self) -> List[PreMediaContainer]:
        assert self.helper is not None

        return self.helper.download_content()

    @with_timing("Instantiating & Calculating file object")
    def make_files(self, files: List[PreMediaContainer]) -> List[MediaContainer]:
        assert self.helper is not None
        assert self.helper.session is not None

        new_files = [MediaContainer.from_pre_container(file, self.helper.session) for file in files]
        filtered_files = [item for item in new_files if item is not None]

        return filtered_files

    @with_timing("Downloading files")
    def download_files(self, files: List[MediaContainer]) -> None:
        def download(file: MediaContainer) -> None:
            assert status is not None
            if enable_multithread:
                thread_id = int(threading.current_thread().name.split("T_")[-1])
            else:
                thread_id = 0

            status.add(thread_id, file)
            file.download()
            status.finish(thread_id)

        if enable_multithread:
            with ThreadPoolExecutor(args.num_threads, thread_name_prefix="T") as ex:
                list(ex.map(download, files))
        else:
            for file in files:
                download(file)

    @staticmethod
    @on_kill()
    def shutdown_running_downloads(*_: Any) -> None:
        if downloading_files is None:
            return

        for item in downloading_files:
            item.stop()

        if status is not None:
            status.shutdown()

        # Now wait for the downloads to finish
        while not all(item.done for item in downloading_files):
            time.sleep(0.25)


status: Optional[Status] = None
downloading_files: Optional[List[MediaContainer]] = None