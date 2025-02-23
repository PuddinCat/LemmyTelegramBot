"""通过RSS爬取lemmy实例上的post并发布在telegram上
"""

import logging
import re
import json
import asyncio
import os
from io import BytesIO
from typing import List, Dict, Literal, Union
from traceback import print_exc
import requests
from bs4 import BeautifulSoup
import minify_html
import feedparser

from telegram import constants, InputMediaPhoto, Bot, error

# from telegram.ext import (
#     filters,
#     ApplicationBuilder,
#     ContextTypes,
#     CommandHandler,
#     MessageHandler,
# )
from telegram.error import TelegramError

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)


Post = Union[
    Dict[Literal["guid", "title", "link", "description", "comments"], str],
    Dict[
        Literal["guid", "title", "link", "description", "comments", "image"],
        str,
    ],
]
ScraperState = Dict[Literal["visited"], List[str]]


class HTTPFailed(Exception):
    """HTTP请求错误"""


class ParseFeedFailed(Exception):
    """解析信息错误"""


HTML_MESSAGE = """
<b>{title}</b>
---
{desc}

"""


def html_minify(html_doc: str) -> str:
    """将HTML中多余的标签去除以便telegram渲染

    Args:
        html_doc (str): 需要处理的html

    Returns:
        str: 处理结果
    """
    logging.info("Minify documents with length of %d", len(html_doc))
    soup = BeautifulSoup(minify_html.minify(html_doc), "html.parser")
    for _ in range(len(html_doc)):
        soup_length = len(str(soup))
        for tag in soup.find_all(True)[::-1]:
            child = list(tag.children)
            if tag.name == "br":
                tag.replace_with("\n")
            elif tag.name == "p":
                tag.replace_with("\n" + tag.text.strip())
            elif tag.name == "li":
                tag.replace_with(tag.text.strip() + child[0].text.strip())
            elif tag.name in ["a", "b", "strong", "i", "em"]:
                continue
            elif not child:
                tag.replace_with(tag.text.strip())
            elif len(child) == 1:
                tag.replace_with(child[0])
        if len(str(soup)) == soup_length:
            break
    html_str = str(soup)
    return html_str


class Scraper:
    """RSS爬虫"""

    def __init__(self, url: str, state=None):
        self.url = url
        self.state: ScraperState = (
            state if state is not None else {"visited": []}
        )

    def filter_posts(self, posts: List[Post]) -> List[Post]:
        """过滤掉已经爬取的post

        Args:
            posts (List[Post]): 需要过滤的posts

        Returns:
            List[Post]: 过滤结果
        """
        return [
            post
            for post in posts
            if post["guid"] not in self.state["visited"]
        ]

    def record_posts(self, posts: List[Post]):
        """将一系列post标记为已经爬取，去除太早期的记录

        Args:
            posts (List[Post]): 需要标记的post
        """
        self.state["visited"] += [post["guid"] for post in posts]
        self.state["visited"] = self.state["visited"][-500:]

    def fetch_new_posts(self) -> List[Post]:
        """获取新的posts

        Raises:
            HTTPFailed: HTTP失败
            ParseFeedFailed: 解析失败

        Returns:
            List[Post]: 爬取到的post
        """
        try:
            resp = requests.get(
                self.url,
                headers={
                    "User-Agent": "RssScraper/0.1 (Maintained by puddin) "
                },
                timeout=10,
            )
        except Exception as exc:
            raise HTTPFailed() from exc
        if resp.status_code != 200:
            raise HTTPFailed(f"Wrong Status Code: {resp.status_code}")
        try:
            rss = feedparser.parse(resp.text)
        except Exception as exc:
            raise ParseFeedFailed() from exc
        return [
            {
                "guid": item["id"],
                "title": item["title"],
                "link": item["link"],
                "description": item["summary"],
                "comments": item["comments"],
            }
            for item in rss.entries
        ]

    def find_image(self, post: Post) -> str | None:
        """找到一个post中的图片

        Args:
            post (Post): Post

        Returns:
            str | None: 图片链接
        """
        desc = post["description"]
        doc = BeautifulSoup(desc, "html.parser")
        for element in doc.find_all("a"):
            if "href" not in element.attrs:
                continue
            if (
                re.search(
                    r"\.(jpg|jpeg|png|gif|webp)$", element.attrs["href"]
                )
                or "imgur" in element.attrs["href"]
            ):
                return element.attrs["href"]
        return None

    def new_posts(self) -> List[Post]:
        """获取当前的所有post，寻找其中的图片并更新当前状态

        Returns:
            List[Post]: 所有Post
        """
        posts = self.fetch_new_posts()
        logging.info("There's %d posts", len(posts))
        posts = self.filter_posts(posts)
        logging.info("There's %d new posts", len(posts))

        self.record_posts(posts)
        for data in posts:
            data["description"] = html_minify(data["description"])
        for post in posts:
            post["image"] = self.find_image(post)
        logging.info("Done finding new posts")
        return posts

    def save(self, file):
        """保存状态到文件中

        Args:
            file (File): 打开的文件，不会主动关闭
        """
        json.dump(self.state, file, indent=2)

    def load(self, file):
        """从文件中加载状态

        Args:
            fp (_type_): 文件
        """
        self.state = json.load(file)

    def refuse(self, post: Post):
        """从状态中删除之前爬取过的Post

        Args:
            post (Post): 需要删除的Post
        """
        if post["guid"] in self.state["visited"]:
            self.state["visited"].remove(post["guid"])


async def send_post(bot: Bot, chat_id: int | str, post: Post) -> bool:
    """发送post到telegram

    Args:
        bot (Bot): telegram bot实例
        chat_id (int | str): 发送目标
        post (Post): 需要发送的post

    Returns:
        bool: 是否发送成功
    """
    try:
        if "image" in post:
            resp = requests.get(post["image"], timeout=60)
            await bot.send_media_group(
                chat_id=chat_id,
                media=[
                    InputMediaPhoto(
                        BytesIO(resp.content),
                        caption=HTML_MESSAGE.format(
                            title=post["title"], desc=post["description"]
                        ),
                        parse_mode=constants.ParseMode.HTML,
                    )
                ],
            )  # type: ignore
        else:
            await bot.send_message(
                chat_id=chat_id,
                text=HTML_MESSAGE.format(
                    title=post["title"], desc=post["description"]
                ),
                parse_mode=constants.ParseMode.HTML,
                disable_web_page_preview=True,
            )  # type: ignore
    except error.TimedOut:
        return False
    except TelegramError:
        print_exc()
        return False
    except Exception:
        print_exc()
        return False
    return True


async def tick(
    bot: Bot,
    scraper: Scraper,
    chat_id: str | int,
):
    """爬取一次并发布

    Args:
        bot (Bot): telegram bot实例
        scraper (Scraper): 爬虫示例
        chat_id (str | int, optional): 发送目标.
        sleep_time (int, optional): 睡眠时间. Defaults to 300.
    """
    posts = None
    try:
        posts = scraper.new_posts()
    except Exception:
        print_exc()
        return
    for post in posts:
        logging.info("Sending post %s", post["guid"])
        result = await send_post(bot, chat_id, post)
        if not result:
            scraper.refuse(post)
    with open("scraper.json", "w", encoding="utf-8") as file:
        scraper.save(file)


def main():
    scraper = Scraper("https://lemmy.ml/feeds/c/programmerhumor.xml?sort=New")
    with open("scraper.json", "r", encoding="utf-8") as file:
        scraper.load(file)
    bot = Bot(os.environ["TELEGRAM_BOT_TOKEN"])
    asyncio.run(tick(bot, scraper, chat_id="@programmer_humor_lemmy_ml"))


if __name__ == "__main__":
    main()
    # test()
