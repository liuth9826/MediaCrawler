# -*- coding: utf-8 -*-
"""把帖子图片抓到本地，供模型读取。

复用 `media_downloader`（SDD §7 复用清单），不另起一套下载逻辑。这里只做两件
它不做的事：

1. **上限**：只取 `image_list` 顺序的前 N 张（小红书封面在前）。这是成本闸门 ——
   一个帖子可能有十几张图，全送模型既贵又没有必要。
2. **诚实**：记住实际抓到的是哪几张，且编号保持原列表序号。证据里的图号必须能对回
   原始 `image_list`，否则「模型看了第 3 张」这句话就无从核对。

下载失败即排除：不重试到天荒地老，也不拿别的图冒充。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from media_downloader.downloader import MediaDownloader
from media_downloader.types import MediaItem, MediaType

from trend.config import VISION_IMAGE_DIR

# 小红书图片 CDN 会校验 Referer；不给会被 403。
XHS_IMAGE_REFERER = "https://www.xiaohongshu.com/"


@dataclass(frozen=True)
class DownloadedImage:
    """一张抓下来的图。`index` 是它在原始 `image_list` 里的 1-based 序号。"""

    index: int
    url: str
    path: Path
    data: bytes


def build_image_downloader(
    *,
    platform: str = "xhs",
    base_dir: Path | str | None = None,
    max_retries: int = 2,
    retry_base_delay: float = 0.5,
) -> MediaDownloader:
    """造一个下载器。显式传 `base_dir` 与 headers，不依赖 config 的隐式回退 ——
    趋势层的图片缓存必须落在自己的目录下，不能混进采集器的存档目录。
    """
    return MediaDownloader(
        platform,
        base_dir=Path(base_dir) if base_dir is not None else VISION_IMAGE_DIR,
        extra_headers={"Referer": XHS_IMAGE_REFERER},
        max_retries=max(0, max_retries),
        retry_base_delay=retry_base_delay,
        retry_max_delay=max(1.0, retry_base_delay * 8),
    )


async def fetch_note_images(
    note_id: str,
    urls: Sequence[str],
    *,
    downloader: MediaDownloader,
    max_images: int,
) -> tuple[DownloadedImage, ...]:
    """抓取前 `max_images` 张图。**不抛异常**：抓不到的图就是没有这张图。

    返回的 `index` 是原列表序号而非「第几个成功的」—— 中间某张失败时，后面的图
    仍带着自己的真实编号，证据不会错位。
    """
    if max_images <= 0:
        return ()

    images: list[DownloadedImage] = []
    for position, url in enumerate(list(urls)[:max_images], start=1):
        item = MediaItem(
            url=url,
            media_type=MediaType.IMAGE,
            content_id=note_id,
            stem=f"{position:02d}",
        )
        path = await downloader.download(item)
        if path is None:
            continue
        try:
            data = path.read_bytes()
        except OSError:
            # 文件在下载与读取之间消失（磁盘满、被清理）—— 当作这张图没拿到。
            continue
        images.append(DownloadedImage(index=position, url=url, path=path, data=data))
    return tuple(images)
