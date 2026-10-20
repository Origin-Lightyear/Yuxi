from yuxi.storage.postgres.models_business import IMBotConfig


def test_im_bot_to_dict_hides_secrets_and_defaults_allowed_users():
    bot = IMBotConfig(
        platform="feishu",
        name="demo",
        owner_uid="u1",
        agent_slug="default-chatbot",
        credentials_encrypted="enc",
        api_key_cipher="cipher",
        allowed_users=None,
        require_mention=True,
        group_sessions_per_user=True,
    )
    data = bot.to_dict()
    assert data["platform"] == "feishu"
    assert data["allowed_users"] == []
    assert data["require_mention"] is True
    assert data["group_sessions_per_user"] is True
    assert "credentials_encrypted" not in data
    assert "api_key_cipher" not in data
