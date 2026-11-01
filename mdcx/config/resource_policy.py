from dataclasses import dataclass
from typing import TYPE_CHECKING

from .enums import DownloadableFile, KeepableFile, NfoInclude, ReadMode
from .manager import manager

if TYPE_CHECKING:
    from .models import Config


@dataclass(frozen=True)
class ResourcePolicy:
    downloadable: DownloadableFile
    keepable: KeepableFile
    download_files: list[DownloadableFile]
    keep_files: list[KeepableFile]

    @property
    def should_download(self) -> bool:
        return self.downloadable in self.download_files

    @property
    def should_keep(self) -> bool:
        return self.keepable in self.keep_files

    @property
    def should_remove_existing(self) -> bool:
        return not self.should_download and not self.should_keep


def resource_policy(
    downloadable: DownloadableFile,
    keepable: KeepableFile,
    *,
    download_files: list[DownloadableFile],
    keep_files: list[KeepableFile],
) -> ResourcePolicy:
    return ResourcePolicy(downloadable, keepable, download_files, keep_files)


def is_nfo_output_enabled(config: "Config | None" = None) -> bool:
    cfg = config if config is not None else manager.config
    if cfg.main_mode == 2:
        return False
    if DownloadableFile.NFO in cfg.download_files:
        return True
    return cfg.main_mode == 4 and ReadMode.HAS_NFO_UPDATE in cfg.read_mode and ReadMode.READ_UPDATE_NFO in cfg.read_mode


def is_poster_output_used(config: "Config | None" = None) -> bool:
    cfg = config if config is not None else manager.config
    if DownloadableFile.POSTER in cfg.download_files:
        return True
    return is_nfo_output_enabled(cfg) and NfoInclude.POSTER in cfg.nfo_include_new


def is_thumb_output_used(config: "Config | None" = None) -> bool:
    cfg = config if config is not None else manager.config
    if any(
        item in cfg.download_files
        for item in (DownloadableFile.THUMB, DownloadableFile.FANART, DownloadableFile.POSTER)
    ):
        return True
    return is_nfo_output_enabled(cfg) and NfoInclude.COVER in cfg.nfo_include_new


def is_trailer_output_used(config: "Config | None" = None) -> bool:
    cfg = config if config is not None else manager.config
    if DownloadableFile.TRAILER in cfg.download_files:
        return True
    return is_nfo_output_enabled(cfg) and NfoInclude.TRAILER in cfg.nfo_include_new
