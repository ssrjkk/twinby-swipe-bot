import logging
from typing import Annotated, Literal, TypedDict

from pydantic import BaseModel, Field

from ai.universal_llm import UniversalAIGateway

try:
    from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
    from langgraph.graph import END, START, StateGraph
    from langgraph.graph.message import add_messages
    HAS_LANGGRAPH = True
except ImportError:
    HAS_LANGGRAPH = False

logger = logging.getLogger(__name__)


class StrategistOutput(BaseModel):
    stage: Literal["icebreaker", "rapport", "closing", "handoff"] = "rapport"
    tactic: str = Field(default="легкий юмор")
    draft_reply: str
    should_handoff: bool = False
    handoff_summary: str = ""


class HumanizerOutput(BaseModel):
    approved: bool = True
    critique: str = ""
    messages_chunks: list[str] = Field(min_length=1)


if HAS_LANGGRAPH:
    class DialogueState(TypedDict):
        messages: Annotated[list[BaseMessage], add_messages]
        profile_context: str
        visual_hooks: list[str]
        psychological_profile: str
        stage: str
        draft_reply: str
        critique: str
        revision_count: int
        final_chunks: list[str]
        should_handoff: bool
        handoff_summary: str
else:
    class DialogueState(TypedDict):  # type: ignore[no-redef]
        messages: list[dict[str, str]]
        profile_context: str
        visual_hooks: list[str]
        psychological_profile: str
        stage: str
        draft_reply: str
        critique: str
        revision_count: int
        final_chunks: list[str]
        should_handoff: bool
        handoff_summary: str


class DatingLangGraphEngine:
    """Реальный 3-узловой агент (Profiler -> Strategist -> Humanizer) поверх UniversalAIGateway
    с циклом ревизий и очисткой нейросетевых штампов.
    """

    def __init__(self, gateway: UniversalAIGateway | None = None) -> None:
        self.gateway = gateway or UniversalAIGateway()
        self.graph = self._build_graph() if HAS_LANGGRAPH else None

    @staticmethod
    def _format_history(messages: list) -> str:
        lines: list[str] = []
        for m in messages[-8:]:
            if isinstance(m, dict):
                lines.append(f"{m.get('role', 'user')}: {m.get('content', '')}")
            else:
                lines.append(f"{getattr(m, 'type', 'user')}: {getattr(m, 'content', '')}")
        return "\n".join(lines) or "[Диалог еще не начат — нужен первый icebreaker]"

    async def _profiler_node(self, state: DialogueState) -> dict:
        prompt = (
            "Проанализируй анкету, визуальные детали с фото и историю переписки. "
            "Кратко (2-3 предложения) опиши: язык общения, манеру письма (длина фраз, регистр, эмодзи) "
            "и уровень вовлечённости собеседницы.\n\n"
            f"Анкета: {state['profile_context']}\n"
            f"Детали с фото: {', '.join(state['visual_hooks']) or 'нет'}\n"
            f"История:\n{self._format_history(state['messages'])}"
        )
        summary = await self.gateway.complete_chat(
            messages=[
                {"role": "system", "content": "Ты — поведенческий психолог и аналитик диалогов."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.4,
            json_mode=False,
        )
        return {"psychological_profile": summary.strip()}

    async def _strategist_node(self, state: DialogueState) -> dict:
        critique_note = (
            f"\nОБЯЗАТЕЛЬНО ИСПРАВЬ замечание редактора: {state['critique']}"
            if state.get("critique")
            else ""
        )
        sys_prompt = (
            "Ты — мастер живого общения в дейтинге. Веди диалог на языке собеседницы "
            "и за 4-7 обменов плавно переведи в Telegram или позови на кофе. "
            "Не задавай больше одного вопроса за раз. Если она уже дала контакт или согласилась на встречу — "
            "ставь should_handoff=true и заполни handoff_summary.\n"
            "Верни СТРОГО JSON с полями: stage (icebreaker|rapport|closing|handoff), tactic, draft_reply, "
            "should_handoff (bool), handoff_summary."
        )
        user_prompt = (
            f"Психологический портрет: {state['psychological_profile']}\n"
            f"Анкета: {state['profile_context']}\n"
            f"Детали с фото: {', '.join(state['visual_hooks']) or 'нет'}\n"
            f"История:\n{self._format_history(state['messages'])}"
            f"{critique_note}"
        )
        raw = await self.gateway.complete_json(
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.75,
        )
        parsed = StrategistOutput.model_validate(raw)
        return {
            "stage": parsed.stage,
            "draft_reply": parsed.draft_reply,
            "should_handoff": parsed.should_handoff,
            "handoff_summary": parsed.handoff_summary,
            "revision_count": state.get("revision_count", 0) + 1,
        }

    async def _humanizer_node(self, state: DialogueState) -> dict:
        sys_prompt = (
            "Ты — редактор живой переписки в мессенджере. Проверь черновик ответа:\n"
            "1. Убери ИИ-штампы: длинные тире (—), канцелярит, пафосные комплименты, шаблон 'Комплимент + Вопрос'.\n"
            "2. Подстрой пунктуацию и регистр под стиль собеседницы.\n"
            "3. Разбей ответ на 1-3 коротких живых сообщения (messages_chunks).\n"
            "Если черновик всё равно звучит как робот — верни approved=false и напиши critique.\n"
            "Верни СТРОГО JSON: approved (bool), critique (str), messages_chunks (list[str])."
        )
        user_prompt = (
            f"Стиль собеседницы: {state['psychological_profile']}\n"
            f"Черновик: {state['draft_reply']}"
        )
        raw = await self.gateway.complete_json(
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.5,
        )
        parsed = HumanizerOutput.model_validate(raw)
        # Страховка от длинных тире на уровне кода
        cleaned_chunks = [c.replace(" — ", " - ").replace("—", "-").strip() for c in parsed.messages_chunks if c.strip()]
        return {
            "critique": "" if parsed.approved else parsed.critique,
            "final_chunks": cleaned_chunks or [state["draft_reply"].replace("—", "-").strip()],
        }

    def _should_revise(self, state: DialogueState) -> Literal["strategist", "__end__"]:
        if state.get("critique") and state.get("revision_count", 0) < 2:
            return "strategist"
        return "__end__"

    def _build_graph(self):
        builder = StateGraph(DialogueState)
        builder.add_node("profiler", self._profiler_node)
        builder.add_node("strategist", self._strategist_node)
        builder.add_node("humanizer", self._humanizer_node)
        builder.add_edge(START, "profiler")
        builder.add_edge("profiler", "strategist")
        builder.add_edge("strategist", "humanizer")
        builder.add_conditional_edges("humanizer", self._should_revise, {"strategist": "strategist", "__end__": END})
        return builder.compile()

    async def run_turn(
        self,
        history: list[dict[str, str]],
        profile_context: str,
        visual_hooks: list[str] | None = None,
    ) -> dict:
        if HAS_LANGGRAPH and self.graph is not None:
            lc_messages = [AIMessage(content=m["content"]) if m.get("role") == "assistant" else HumanMessage(content=m["content"]) for m in history]
            res = await self.graph.ainvoke(
                {
                    "messages": lc_messages,
                    "profile_context": profile_context,
                    "visual_hooks": visual_hooks or [],
                    "psychological_profile": "",
                    "stage": "rapport",
                    "draft_reply": "",
                    "critique": "",
                    "revision_count": 0,
                    "final_chunks": [],
                    "should_handoff": False,
                    "handoff_summary": "",
                }
            )
            return {
                "chunks": res["final_chunks"],
                "stage": res["stage"],
                "should_handoff": res["should_handoff"],
                "handoff_summary": res["handoff_summary"],
            }

        # Fallback-выполнение того же графа, если langgraph не установлен локально
        state: DialogueState = {
            "messages": list(history),
            "profile_context": profile_context,
            "visual_hooks": visual_hooks or [],
            "psychological_profile": "",
            "stage": "rapport",
            "draft_reply": "",
            "critique": "",
            "revision_count": 0,
            "final_chunks": [],
            "should_handoff": False,
            "handoff_summary": "",
        }
        state.update(await self._profiler_node(state))
        while True:
            state.update(await self._strategist_node(state))
            state.update(await self._humanizer_node(state))
            if self._should_revise(state) == "__end__":
                break
        return {
            "chunks": state["final_chunks"],
            "stage": state["stage"],
            "should_handoff": state["should_handoff"],
            "handoff_summary": state["handoff_summary"],
        }
