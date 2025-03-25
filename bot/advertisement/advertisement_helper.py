# bot/subscription/subscription_manager.py
import os
from datetime import datetime
from aiogram import Bot, exceptions
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from bot.utils.log import log_action


class SubscriptionManager:
    def __init__(self):
        self.current_channels = []
        self.last_checked = datetime.now()

    async def initialize(self):
        self.load_channels()
        if not os.path.exists('channels.txt'):
            open('channels.txt', 'w').close()
            log_action("Создан файл channels.txt")

    def load_channels(self):
        try:
            with open('channels.txt', 'r') as f:
                channels = []
                for line in f:
                    line = line.strip()
                    if line:
                        channel = line if line.startswith('@') else f'@{line}'
                        channels.append(channel)
                self.current_channels = channels
        except FileNotFoundError:
            self.current_channels = []

    async def is_subscribed(self, user_id: int, channel_username: str, bot: Bot) -> bool:
        try:
            chat = await bot.get_chat(chat_id=channel_username.lstrip('@'))
            member = await bot.get_chat_member(chat_id=chat.id, user_id=user_id)
            return member.status in ['member', 'administrator', 'creator']
        except exceptions.TelegramBadRequest as e:
            if "chat not found" in str(e):
                log_action("Ошибка канала", f"Канал {channel_username} не найден!")
            return True
        except Exception as e:
            log_action("Ошибка подписки", f"{user_id} {channel_username}: {str(e)}")
            return False

    async def check_subscription_handler(self, query: CallbackQuery):
        try:
            all_subscribed = True
            for channel in self.current_channels:
                if not await self.is_subscribed(query.from_user.id, channel, query.bot):
                    all_subscribed = False
                    break

            if all_subscribed:
                await query.message.edit_text("✅ Спасибо за подписку! Теперь вы можете пользоваться ботом!")
                return True
            else:
                await query.answer("❌ Вы подписались не на все каналы!", show_alert=True)
                return False
        except Exception as e:
            log_action("Ошибка проверки", str(e))
            await query.answer("⚠️ Ошибка при проверке подписки", show_alert=True)

    async def channels_updater(self):
        while True:
            await asyncio.sleep(86400)  # 24 часа
            self.load_channels()
            log_action("Каналы обновлены", f"Всего каналов: {len(self.current_channels)}")


class SubscriptionMiddleware:
    def __init__(self, sub_manager):
        self.sub_manager = sub_manager

    async def __call__(self, handler, event, data):
        if not getattr(event, 'from_user', None):
            return await handler(event, data)

        if isinstance(event, (Message, CallbackQuery)) and self.should_skip_check(event):
            return await handler(event, data)

        user = event.from_user
        bot = data['bot']

        for channel in self.sub_manager.current_channels:
            if not await self.sub_manager.is_subscribed(user.id, channel, bot):
                await self.ask_for_subscription(event, bot)
                raise CancelHandler()

        return await handler(event, data)

    def should_skip_check(self, event):
        if isinstance(event, Message) and event.text == "/start":
            return True
        if isinstance(event, CallbackQuery) and event.data == "check_subscription":
            return True
        return False

    async def ask_for_subscription(self, event, bot: Bot):
        markup = InlineKeyboardMarkup()
        for channel in self.sub_manager.current_channels:
            username = channel.lstrip('@')
            markup.add(InlineKeyboardButton(
                text=f"Подписаться на {username}",
                url=f"https://t.me/{username}"
            ))
        markup.add(InlineKeyboardButton(
            text="✅ Я подписался",
            callback_data="check_subscription"
        ))

        text = "📢 Для использования бота подпишитесь на наши каналы:"
        try:
            if isinstance(event, Message):
                await event.answer(text, reply_markup=markup)
            elif isinstance(event, CallbackQuery):
                await event.message.edit_text(text, reply_markup=markup)
        except Exception as e:
            log_action("Ошибка запроса подписки", str(e))

