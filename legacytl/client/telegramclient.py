from . import (
    AccountMethods,
    AuthMethods,
    DownloadMethods,
    DialogMethods,
    ChatMethods,
    BotMethods,
    MessageMethods,
    UploadMethods,
    ButtonMethods,
    GiftMethods,
    EphemeralMethods,
    UpdateMethods,
    MessageParseMethods,
    UserMethods,
    TelegramBaseClient,
)


class TelegramClient(
    AccountMethods,
    AuthMethods,
    DownloadMethods,
    DialogMethods,
    ChatMethods,
    BotMethods,
    MessageMethods,
    UploadMethods,
    ButtonMethods,
    GiftMethods,
    EphemeralMethods,
    UpdateMethods,
    MessageParseMethods,
    UserMethods,
    TelegramBaseClient,
):
    pass
