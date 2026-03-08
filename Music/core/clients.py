from pyrogram import Client

from config import Config
from Music.utils.exceptions import HellBotException

from .logger import LOGS


class HellClient(Client):
    def __init__(self):
        self.app = Client(
            "HellMusic",
            api_id=Config.API_ID,
            api_hash=Config.API_HASH,
            bot_token=Config.BOT_TOKEN,
            plugins=dict(root="Music.plugins"),
            workers=100,
        )

        self.user1 = Client(
            "HellClient1",
            api_id=Config.API_ID,
            api_hash=Config.API_HASH,
            session_string=Config.HELLBOT_SESSION,
            no_updates=True,
        ) if Config.HELLBOT_SESSION else None

        session2 = getattr(Config, "HELLBOT_SESSION2", None)
        self.user2 = Client(
            "HellClient2",
            api_id=Config.API_ID,
            api_hash=Config.API_HASH,
            session_string=session2,
            no_updates=True,
        ) if session2 else None

        session3 = getattr(Config, "HELLBOT_SESSION3", None)
        self.user3 = Client(
            "HellClient3",
            api_id=Config.API_ID,
            api_hash=Config.API_HASH,
            session_string=session3,
            no_updates=True,
        ) if session3 else None

        # Consolidate active assistants
        self.users = [user for user in [self.user1, self.user2, self.user3] if user]
        # Backwards compatibility for plugins relying on `hellbot.user`
        self.user = self.users[0] if self.users else None

    def get_user(self, chat_id: int):
        """Round-robin assistant assigner based on Chat ID"""
        if not self.users:
            return None
        return self.users[abs(chat_id) % len(self.users)]

    async def start(self):
        LOGS.info("\x3e\x3e\x20\x42\x6f\x6f\x74\x69\x6e\x67\x20\x75\x70\x20\x48\x65\x6c\x6c\x4d\x75\x73\x69\x63\x2e\x2e\x2e")
        if Config.BOT_TOKEN:
            await self.app.start()
            me = await self.app.get_me()
            self.app.id = me.id
            self.app.mention = me.mention
            self.app.name = me.first_name
            self.app.username = me.username
            LOGS.info(f"\x3e\x3e\x20{self.app.name}\x20\x69\x73\x20\x6f\x6e\x6c\x69\x6e\x65\x20\x6e\x6f\x77\x21")
        
        if self.users:
            for i, user in enumerate(self.users):
                await user.start()
                me = await user.get_me()
                user.id = me.id
                user.mention = me.mention
                user.name = me.first_name
                user.username = me.username
                try:
                    await user.join_chat("ArcBotz")
                    await user.join_chat("https://t.me/Updates")
                except:
                    pass
                LOGS.info(f"\x3e\x3e\x20{user.name} (Assistant {i+1}) \x69\x73\x20\x6f\x6e\x6c\x69\x6e\x65\x20\x6e\x6f\x77\x21")
                
        LOGS.info("\x3e\x3e\x20\x42\x6f\x6f\x74\x65\x64\x20\x75\x70\x20\x48\x65\x6c\x6c\x4d\x75\x73\x69\x63\x21")

    async def logit(self, hash: str, log: str, file: str = None):
        log_text = f"#{hash.upper()} \n\n{log}"
        try:
            if file:
                await self.app.send_document(
                    Config.LOGGER_ID, file, caption=log_text
                )
            else:
                await self.app.send_message(
                    Config.LOGGER_ID, log_text, disable_web_page_preview=True
                )
        except Exception as e:
            raise HellBotException(f"[HellBotException]: {e}")

hellbot = HellClient()
