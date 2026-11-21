from ai.bandits import PromptThompsonBandit
from ai.langgraph_agent import DatingLangGraphEngine
from ai.universal_llm import UniversalAIGateway


class AutonomousChatAgent:
    """Высокоуровневый фасад диалогового агента: LangGraph + Thompson Sampling Bandit."""

    def __init__(
        self,
        gateway: UniversalAIGateway | None = None,
        bandit: PromptThompsonBandit | None = None,
    ) -> None:
        self.engine = DatingLangGraphEngine(gateway=gateway)
        self.bandit = bandit or PromptThompsonBandit()

    async def generate_reply(
        self,
        history: list[dict[str, str]],
        profile_context: str,
        visual_hooks: list[str] | None = None,
    ) -> dict:
        has_our_reply = any(m.get("role") == "assistant" for m in history)
        chosen_arm = await self.bandit.select_arm() if not has_our_reply else None

        enriched_context = profile_context
        if chosen_arm:
            enriched_context = f"{profile_context}\n[Рекомендуемый стиль первого ответа: {chosen_arm}]"

        res = await self.engine.run_turn(
            history=history,
            profile_context=enriched_context,
            visual_hooks=visual_hooks,
        )
        res["bandit_arm"] = chosen_arm
        return res
