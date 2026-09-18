"""Qt page widgets owned by the package GUI boundary."""

from .history import HistoryPage
from .home import HomePage
from .image import ImagePage
from .inflight import InflightPage
from .mixed import MixedPage
from .settings import SettingsPage
from .task import TaskPage
from .upload import UploadPage, UploadPageServices
from .video import VideoPage

__all__ = [
    "HistoryPage",
    "HomePage",
    "ImagePage",
    "InflightPage",
    "MixedPage",
    "SettingsPage",
    "TaskPage",
    "UploadPage",
    "UploadPageServices",
    "VideoPage",
]
