from yuxi.agents.presets import AgentPreset

PRESET = AgentPreset(
    slug="default-chatbot",
    name="智能助手",
    description="基础的对话机器人，可以回答问题，可在配置中启用需要的工具。",
    backend_id="ChatbotAgent",
    scene="general",
    icon="message-circle",
    example_questions=(
        "帮我把这段话改写得更简洁专业",
        "解释一下什么是向量数据库",
        "给周末城市徒步推荐三条路线",
    ),
    tips=(
        "在配置中启用联网或知识库工具可获得更准确的回答。",
        "提供背景信息和期望的输出格式能让回答更贴合需求。",
    ),
)
