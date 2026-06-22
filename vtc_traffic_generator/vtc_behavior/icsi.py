from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True)
class ICSITimelineEvent:
    meeting_id: str
    speaker_id: str
    bot_index: int
    channel: str
    start_sec: float
    end_sec: float
    dialogue_act_type: str
    file_channel: str

    @property
    def duration_sec(self) -> float:
        return self.end_sec - self.start_sec


class ICSIReplayPolicy:
    """Replay one ICSI meeting's dialogue-act timeline without mixing meetings."""

    def __init__(
        self,
        corpus_root: str | None,
        bot_count: int,
        meeting_id: str | None = None,
        dialogue_acts_dir: str | None = None,
        min_speaker_duration_sec: float = 1.0,
        min_utterance_duration_sec: float = 0.05,
    ):
        if bot_count < 3:
            raise ValueError("ICSI mode supports 3 or more bots only.")

        self.project_root = Path(__file__).resolve().parents[2]
        self.corpus_root = self._resolve_path(corpus_root) if corpus_root else self._default_corpus_root()
        self.dialogue_acts_dir = self._resolve_dialogue_acts_dir(dialogue_acts_dir)
        self.bot_count = bot_count
        self.requested_meeting_id = meeting_id
        self.min_speaker_duration_sec = min_speaker_duration_sec
        self.min_utterance_duration_sec = min_utterance_duration_sec

        if not self.dialogue_acts_dir.exists():
            expected = [
                self.corpus_root / "ICSI" / "DialogueActs",
                self.corpus_root / "DialogueActs",
            ]
            raise FileNotFoundError(
                "Missing ICSI DialogueActs directory. Checked: "
                + ", ".join(str(path) for path in expected)
            )

        self.meeting_id = self._select_meeting_id()
        self.speaker_totals = self._speaker_totals(self.meeting_id)
        self.active_speakers = self._active_speakers(self.speaker_totals)
        self.speaker_to_bot_index = self._map_speakers_to_bots(self.active_speakers)
        self.events = self._load_events()

        if not self.events:
            raise ValueError(f"ICSI meeting {self.meeting_id} has no usable dialogue acts.")

    @classmethod
    def from_config(cls, config: Mapping[str, Any], bot_count: int) -> "ICSIReplayPolicy":
        behavior = config.get("behavior", {})
        if not isinstance(behavior, Mapping):
            behavior = {}

        icsi = behavior.get("icsi", {})
        if not isinstance(icsi, Mapping):
            icsi = {}

        corpus_root = icsi.get("corpus_root") or behavior.get("icsi_corpus_root")

        return cls(
            corpus_root=str(corpus_root) if corpus_root else None,
            bot_count=bot_count,
            meeting_id=icsi.get("meeting_id"),
            dialogue_acts_dir=icsi.get("dialogue_acts_dir"),
            min_speaker_duration_sec=float(icsi.get("min_speaker_duration_sec", 1.0)),
            min_utterance_duration_sec=float(icsi.get("min_utterance_duration_sec", 0.05)),
        )

    def _default_corpus_root(self) -> Path:
        return self.project_root / "media" / "icsi"

    def _resolve_path(self, value: str) -> Path:
        path = Path(value).expanduser()
        if path.is_absolute():
            return path
        return self.project_root / path

    def _resolve_dialogue_acts_dir(self, configured_dir: str | None) -> Path:
        if configured_dir:
            return self._resolve_path(configured_dir)

        for path in (
            self.corpus_root / "ICSI" / "DialogueActs",
            self.corpus_root / "DialogueActs",
        ):
            if path.exists():
                return path

        return self.corpus_root / "ICSI" / "DialogueActs"

    @property
    def start_sec(self) -> float:
        return self.events[0].start_sec

    @property
    def end_sec(self) -> float:
        return max(event.end_sec for event in self.events)

    def summary(self) -> dict[str, Any]:
        return {
            "meeting_id": self.meeting_id,
            "active_speakers": self.active_speakers,
            "speaker_to_bot_index": self.speaker_to_bot_index,
            "event_count": len(self.events),
            "start_sec": self.start_sec,
            "end_sec": self.end_sec,
        }

    def _select_meeting_id(self) -> str:
        meeting_ids = self._meeting_ids()
        if self.requested_meeting_id:
            if self.requested_meeting_id not in meeting_ids:
                raise ValueError(f"ICSI meeting not found: {self.requested_meeting_id}")

            active_count = self._active_speaker_count(self.requested_meeting_id)
            if active_count != self.bot_count:
                raise ValueError(
                    f"ICSI meeting {self.requested_meeting_id} has {active_count} active speakers, "
                    f"but {self.bot_count} bots were requested."
                )
            return self.requested_meeting_id

        matching = [
            meeting_id
            for meeting_id in meeting_ids
            if self._active_speaker_count(meeting_id) == self.bot_count
        ]
        if not matching:
            raise ValueError(f"No ICSI meeting has exactly {self.bot_count} active speakers.")

        return sorted(matching)[0]

    def _meeting_ids(self) -> set[str]:
        meeting_ids = set()
        for path in self.dialogue_acts_dir.glob("*.dialogue-acts.xml"):
            parsed = self._parse_dialogue_act_filename(path.name)
            if parsed:
                meeting_ids.add(parsed[0])
        return meeting_ids

    def _active_speaker_count(self, meeting_id: str) -> int:
        return len(self._active_speakers(self._speaker_totals(meeting_id)))

    def _speaker_totals(self, meeting_id: str) -> dict[str, float]:
        totals = defaultdict(float)
        for path in self._meeting_files(meeting_id):
            for dialogue_act in self._iter_dialogue_acts(path):
                duration = dialogue_act["end_sec"] - dialogue_act["start_sec"]
                if duration >= self.min_utterance_duration_sec:
                    totals[dialogue_act["speaker_id"]] += duration
        return dict(totals)

    def _active_speakers(self, speaker_totals: Mapping[str, float]) -> list[str]:
        return sorted(
            [
                speaker_id
                for speaker_id, total_sec in speaker_totals.items()
                if total_sec >= self.min_speaker_duration_sec
            ],
            key=lambda speaker_id: (-speaker_totals[speaker_id], speaker_id),
        )

    def _map_speakers_to_bots(self, active_speakers: list[str]) -> dict[str, int]:
        if len(active_speakers) != self.bot_count:
            raise ValueError(
                f"ICSI meeting {self.meeting_id} has {len(active_speakers)} active speakers, "
                f"but {self.bot_count} bots were requested."
            )
        return {speaker_id: index for index, speaker_id in enumerate(active_speakers)}

    def _load_events(self) -> list[ICSITimelineEvent]:
        events = []
        for path in self._meeting_files(self.meeting_id):
            for dialogue_act in self._iter_dialogue_acts(path):
                speaker_id = dialogue_act["speaker_id"]
                if speaker_id not in self.speaker_to_bot_index:
                    continue

                duration = dialogue_act["end_sec"] - dialogue_act["start_sec"]
                if duration < self.min_utterance_duration_sec:
                    continue

                events.append(
                    ICSITimelineEvent(
                        meeting_id=self.meeting_id,
                        speaker_id=speaker_id,
                        bot_index=self.speaker_to_bot_index[speaker_id],
                        channel=dialogue_act["channel"],
                        start_sec=dialogue_act["start_sec"],
                        end_sec=dialogue_act["end_sec"],
                        dialogue_act_type=dialogue_act["dialogue_act_type"],
                        file_channel=dialogue_act["file_channel"],
                    )
                )

        return sorted(events, key=lambda event: (event.start_sec, event.end_sec, event.speaker_id))

    def _meeting_files(self, meeting_id: str) -> list[Path]:
        return sorted(self.dialogue_acts_dir.glob(f"{meeting_id}.*.dialogue-acts.xml"))

    def _iter_dialogue_acts(self, path: Path):
        parsed = self._parse_dialogue_act_filename(path.name)
        if not parsed:
            return

        meeting_id, channel_from_filename = parsed
        tree = ET.parse(path)
        for element in tree.iter("dialogueact"):
            start_sec = float(element.attrib["starttime"])
            end_sec = float(element.attrib["endtime"])
            speaker_id = element.attrib.get("participant") or channel_from_filename
            channel = element.attrib.get("channel") or channel_from_filename
            dialogue_act_type = element.attrib.get("type") or element.attrib.get("original-type") or "unknown"

            yield {
                "meeting_id": meeting_id,
                "speaker_id": speaker_id,
                "channel": channel,
                "file_channel": channel_from_filename,
                "start_sec": start_sec,
                "end_sec": end_sec,
                "dialogue_act_type": dialogue_act_type,
            }

    def _parse_dialogue_act_filename(self, filename: str) -> tuple[str, str] | None:
        match = re.match(r"^([^.]+)\.([^.]+)\.dialogue-acts\.xml$", filename)
        if not match:
            return None
        return match.group(1), match.group(2)
