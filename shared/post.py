import datetime
from typing import List, Optional
import sqlalchemy as sa
from requests import session
from sqlalchemy.ext.asyncio import AsyncSession
from aiogram.types import InputMediaPhoto
from aiogram import Bot
import config
from src.keyboards import Keyboard
from database.models.posted_history import PostedHistory
from database.models.users import User
from database.models.created_posts import CreatedPosts
from database.models.auto_posts import AutoPosts
from database.models.shcedule import Schedule
from shared.user import BalanceManager, PacketManager
from shared.pricelist import PriceList
import requests
import re


class BasePost:
    def __init__(self, text: str, author_id: int, author_username: str, images: Optional[List[str]] = None,
                 bot_message_id_list: Optional[List[int]] = None, mention_link: Optional[str] = None,
                 posted_id: Optional[int] = None):
        self.text = text
        self.images = images
        self.author_id = author_id
        self.bot_message_id_list = bot_message_id_list
        self.author_username = author_username
        self.mention_link = mention_link or self._generate_mention_link()
        self.posted_id = posted_id

    def _generate_mention_link(self):
        return f"https://t.me/{self.author_username}" if self.author_username else f"tg://user?id={self.author_id}"

    async def send(self, bot: Bot, session: AsyncSession):
        self.posted_id = await self.new_post(session=session)
        print('отправка')
        print(self.posted_id)
        try:
            mention_link = await ShortLink.shorten_links([self.mention_link], self.posted_id, bot.id)
            mention_link = mention_link.get(self.mention_link)
            text = await ShortLink.find_and_shorten_links(self.text, self.posted_id, bot.id)
            self.text = text
            self.mention_link = mention_link
        except Exception as e:
            print(e)

        print(self.mention_link)
        message_id = await self.post_to_chat(bot)
        if isinstance(message_id, list):
            message_id = message_id[0]
        else:
            message_id = message_id.message_id

        await self.set_post_sended(self.posted_id, self.mention_link, message_id, session)

    async def post_to_chat(self, bot: Bot):
        keyboard = Keyboard.chat_post_menu(self.mention_link, 0)
        if self.images:
            if len(self.images) == 1:
                return await bot.send_photo(chat_id=config.chat_id, photo=self.images[0], caption=self.text,
                                            reply_markup=keyboard, parse_mode='html')
            media_group = [
                InputMediaPhoto(media=file_id, caption=self.text if i == 0 else "", parse_mode='html')
                for i, file_id in enumerate(self.images)
            ]
            return await bot.send_media_group(chat_id=config.chat_id, media=media_group)
        return await bot.send_message(chat_id=config.chat_id, text=self.text, reply_markup=keyboard, parse_mode='html',
                                      disable_web_page_preview=True)

    async def new_post(self, session: AsyncSession):
        stmt = sa.insert(PostedHistory).values(
            user_id=self.author_id,
            message_text=self.text,
            message_photo=self.images,
            mention_link=self.mention_link
        ).returning(PostedHistory.id)
        res = await session.execute(stmt)
        await session.commit()
        return res.scalar()

    async def set_post_sended(self, post_id: int, mention_link: str, message_id: int, session: AsyncSession):
        stmt = sa.update(PostedHistory).values(message_id=message_id, mention_link=mention_link).where(PostedHistory.id == post_id)
        await session.execute(stmt)
        await session.commit()

    async def delete(self, session: AsyncSession):
        pass  # Переопределяется в подклассах


class Post(BasePost):
    def __init__(self, post_id: Optional[int] = None, **kwargs):
        super().__init__(**kwargs)
        self.post_id = post_id

    @classmethod
    async def from_db(cls, post_id: int, session: AsyncSession):
        stmt = sa.select(CreatedPosts, User.username).join(User, CreatedPosts.user_id == User.telegram_user_id).where(
            CreatedPosts.id == post_id)
        result = await session.execute(stmt)
        row = result.first()
        if row:
            created_post, username = row
            return cls(post_id=created_post.id, text=created_post.text, author_id=created_post.user_id,
                       author_username=username, images=created_post.images_links,
                       mention_link=created_post.mention_link, bot_message_id_list=created_post.bot_message_id_list)
        return None

    async def create(self, session: AsyncSession):
        stmt = sa.insert(CreatedPosts).values(
            user_id=self.author_id,
            text=self.text,
            images_links=self.images,
            mention_link=self.mention_link
        ).returning(CreatedPosts.id)
        result = await session.execute(stmt)
        self.post_id = result.scalar_one_or_none()
        await session.commit()
        return self.post_id

    async def post(self, session: AsyncSession, bot: Bot):
        active_packet = await PacketManager.has_active_packet(user_id=self.author_id, session=session)
        today_limit = 0
        if active_packet:
            today_limit = await PacketManager.get_today_limit(user_id=self.author_id, session=session)
        if today_limit >= 0:
            success = await PacketManager.deduct_today_limit(user_id=self.author_id, session=session)
        else:
            price = (await PriceList.get_onetime_price(session=session))[0].price
            success = await BalanceManager.deduct(self.author_id, price, session)
        print(success)
        if not success:
            return
        await self.send(bot=bot, session=session)
        await self.delete(session=session)
        return True

    async def delete(self, session: AsyncSession):
        await session.execute(sa.delete(CreatedPosts).where(CreatedPosts.id == self.post_id))
        await session.commit()

    async def add_bot_message_id(self, bot_message_id_list: list, session: AsyncSession):
        """Добавление ID сообщений бота в базу"""
        stmt = (
            sa.update(CreatedPosts)
            .where(CreatedPosts.id == self.post_id)
            .values(bot_message_id_list=bot_message_id_list)
        )
        await session.execute(stmt)
        await session.commit()


class AutoPost(BasePost):
    def __init__(self, auto_post_id: Optional[int] = None, times: Optional[List[datetime.time]] = None, **kwargs):
        super().__init__(**kwargs)
        self.auto_post_id = auto_post_id
        self.times = times

    @classmethod
    async def from_db(cls, auto_post_id: int, session: AsyncSession):
        stmt = sa.select(AutoPosts, User.username).join(User, AutoPosts.user_id == User.telegram_user_id).where(
            AutoPosts.id == auto_post_id)
        result = await session.execute(stmt)
        row = result.first()
        if row:
            auto_post, username = row
            return cls(auto_post_id=auto_post.id, text=auto_post.text, images=auto_post.images_links,
                       times=auto_post.times, author_id=auto_post.user_id, author_username=username,
                       bot_message_id_list=auto_post.bot_message_id_list, mention_link=auto_post.mention_link)
        return None

    async def create(self, session: AsyncSession):
        stmt = sa.insert(AutoPosts).values(
            user_id=self.author_id,
            text=self.text,
            images_links=self.images,
            mention_link=self.mention_link,
            times=self.times,
            activated=0
        ).returning(AutoPosts.id)
        result = await session.execute(stmt)
        self.auto_post_id = result.scalar_one_or_none()
        await session.commit()
        return self.auto_post_id

    async def activate(self, session: AsyncSession):
        """Активация автопоста"""
        await self.delete_active(session)
        await session.execute(
            sa.update(AutoPosts).values(activated=1).where(AutoPosts.id == self.auto_post_id)
        )

        for time in self.times:
            time_parsed = datetime.datetime.strptime(time.strip(), "%H:%M").time()  # Преобразуем в объект time
            current_time = datetime.datetime.now().time()  # Берем только текущее время без даты
            completed = 0

            if time_parsed <= current_time:
                completed = 1

            stmt = sa.insert(Schedule).values(
                user_id=self.author_id,
                scheduled_post_id=self.auto_post_id,
                time=time_parsed,
                completed=completed
            )
            await session.execute(stmt)
        await session.commit()

    async def post(self, bot: Bot, session: AsyncSession):
        today_limit = await PacketManager.get_today_limit(user_id=self.author_id, session=session)
        print(today_limit)

        if int(today_limit) <= 0:
            return
        await PacketManager.deduct_today_limit(user_id=self.author_id, session=session)
        await self.send(bot=bot, session=session)
        return True

    async def delete(self, session: AsyncSession):
        await session.execute(sa.delete(AutoPosts).where(AutoPosts.id == self.auto_post_id))
        await session.commit()

    async def delete_active(self, session: AsyncSession):
        """Удаление активных автопостов пользователя"""
        await session.execute(
            sa.delete(AutoPosts).where(AutoPosts.user_id == self.author_id, AutoPosts.activated == 1)
        )
        await session.commit()

    async def add_bot_message_id(self, bot_message_id_list: list, session: AsyncSession):
        stmt = sa.update(AutoPosts).where(AutoPosts.id == self.auto_post_id).values(
            bot_message_id_list=bot_message_id_list)
        await session.execute(stmt)
        await session.commit()

    @staticmethod
    async def get_auto_post(user_id: int, session: AsyncSession):
        stmt = sa.select(AutoPosts).where(AutoPosts.user_id == user_id, AutoPosts.activated == 1)
        result = await session.execute(stmt)
        r = result.scalar()
        print(r)
        return r

    async def update_time(self, times: list, session: AsyncSession):
        stmt = sa.update(AutoPosts).values(times=times).where(AutoPosts.id == self.auto_post_id)
        stmt2 = sa.delete(Schedule).where(Schedule.scheduled_post_id == self.auto_post_id)
        await session.execute(stmt)
        await session.execute(stmt2)

        for time in times:
            time_parsed = datetime.datetime.strptime(time.strip(), "%H:%M").time()  # Преобразуем в объект time
            current_time = datetime.datetime.now().time()  # Берем только текущее время без даты
            completed = 0

            if time_parsed <= current_time:
                completed = 1

            stmt = sa.insert(Schedule).values(
                user_id=self.author_id,
                scheduled_post_id=self.auto_post_id,
                time=time_parsed,
                completed=completed
            )

            await session.execute(stmt)
        await session.commit()


class ShortLink:
    @staticmethod
    async def shorten_links(links, post_id, bot_id):
        """Функция для шифрования ссылок через API."""
        url = "http://s.forpost.me/shorten"
        payload = {
            "urls": links,
            "post_id": str(post_id),
            "bot_id": str(bot_id)
        }
        headers = {"Content-Type": "application/json"}

        response = requests.post(url, json=payload, headers=headers)

        if response.status_code == 200:
            return {item['original']: item['short'] for item in response.json()}
        else:
            print("Ошибка при получении коротких ссылок:", response.text)
            return {}

    @staticmethod
    async def find_and_shorten_links(text, post_id, bot_id, format_type="html"):
        """Функция находит ссылки и упоминания, делает их кликабельными и сокращает ссылки."""

        # Регулярные выражения
        url_pattern = re.compile(r'https?://[^\s<>"]+')
        mention_pattern = re.compile(r'@(\w+)')

        # Функция замены @mention на кликабельный текст
        def replace_mention(match):
            mention = match.group(1)
            return f'<a href="https://t.me/{mention}">@{mention}</a>' if format_type == "html" else f'[@{mention}](https://t.me/{mention})'

        # Заменяем @mention на кликабельную версию
        text = mention_pattern.sub(replace_mention, text)

        # Находим все ссылки в тексте
        urls = url_pattern.findall(text)
        if not urls:
            return text  # Если ссылок нет, просто возвращаем текст

        # Получаем сокращенные ссылки
        replacement_map = await ShortLink.shorten_links(urls, post_id, bot_id)

        # Функция замены ссылок внутри href
        def replace_href_links(match):
            before, link, after = match.groups()
            new_link = replacement_map.get(link, link)  # Берем сокращенную ссылку, если есть
            return f'{before}{new_link}{after}'

        # 1️⃣ Заменяем ссылки **внутри тегов <a href="...">**
        text = re.sub(r'(<a\s+href=")(https?://[^\s<>"]+)(")', replace_href_links, text)

        # 2️⃣ Заменяем ссылки **в тексте, но НЕ внутри <a href="...">**
        for original, short in replacement_map.items():
            text = re.sub(r'(?<!href=")' + re.escape(original), short, text)
        print(text)
        return text