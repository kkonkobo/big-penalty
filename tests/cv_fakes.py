"""Fictional candidate + fake LLM for the CV tests (no real personal data, no network)."""

from __future__ import annotations

import json

from jobhunt.cv.master import Bullet, Education, Identity, Language, Master, Project, SkillGroup, confirm_master, save_master
from jobhunt.cv.tailor import DraftBullet, DraftProject, DraftSkillGroup, TailorDraft
from jobhunt.cv.verify import Claim, ItemVerdict, VerifyResult
from jobhunt.llm import FailoverLLM, LLMError


def save_confirmed(profile_id: int, master: Master | None = None) -> Master:
    """Active AND confirmed master (what the user gets after clicking "Confirmer ce CV maître")."""
    m = master or sample_master()
    save_master(profile_id, m)
    return confirm_master(profile_id)


def pass_all(call: dict) -> VerifyResult:
    """Default fake verifier: every item is supported."""
    refs = [i["ref"] for i in json.loads(call["user"])["items"]]
    return VerifyResult(items=[ItemVerdict(ref=r, supported=True) for r in refs])


def reject(*refs_and_claims: tuple[str, str], kind: str = "hard_fact"):
    """Fake verifier that flags the given (ref, claim) pairs (as `kind`) and passes the rest."""
    bad = dict(refs_and_claims)

    def out(call: dict) -> VerifyResult:
        refs = [i["ref"] for i in json.loads(call["user"])["items"]]
        return VerifyResult(items=[
            ItemVerdict(ref=r, supported=r not in bad,
                        claims=[Claim(text=bad[r], kind=kind, prep_hint="révise ce point")] if r in bad else [])
            for r in refs])
    return out


def embellish(*refs_and_claims: tuple[str, str]):
    """Fake verifier reporting allowed embellishments (non-blocking, interview prep)."""
    return reject(*refs_and_claims, kind="embellishment")


def sample_master() -> Master:
    return Master(
        identity=Identity(name="Camille Durand", default_title="Élève ingénieure", email="camille@example.test",
                          phone="+33 6 00 00 00 00", linkedin="linkedin.com/in/camille-example", stage_qualifier="4e année / assistant ingénieur"),
        summary="Étudiante en cycle ingénieur en robotique, je conçois des prototypes associant microcontrôleurs, capteurs et actionneurs.",
        skill_groups=[
            SkillGroup(label="Développement logiciel", items=["C", "C++", "Python", "programmation embarquée"]),
            SkillGroup(label="Microcontrôleurs", items=["Arduino Uno (ATmega328P)", "STM32 (ARM Cortex-M)", "GPIO", "ADC", "PWM"]),
            SkillGroup(label="Bus de communication", items=["UART", "SPI", "I2C"]),
            SkillGroup(label="Conception", items=["CAO", "impression 3D", "découpe laser"]),
        ],
        projects=[
            Project(title="Robot suiveur de ligne", dates="2025", context="Projet académique, École Exemple", bullets=[
                Bullet(label="Acquisition", text="lecture de cinq capteurs infrarouges et filtrage des mesures."),
                Bullet(label="Commande", text="régulation de la vitesse de deux moteurs par PWM."),
            ]),
            Project(title="Station météo connectée", dates="2024–2025", context="Projet personnel", bullets=[
                Bullet(label="Mesure", text="lecture d'un capteur de température par I2C et affichage sur écran."),
                Bullet(label="Transmission", text="envoi des données par liaison UART vers un ordinateur."),
            ]),
            Project(title="Boîtier imprimé en 3D", dates="2024", context="Projet académique, École Exemple", bullets=[
                Bullet(label="Conception", text="modélisation 3D et impression d'un boîtier."),
            ]),
        ],
        education=[
            Education(title="Cycle ingénieur - Robotique", school="École Exemple", dates="2024–en cours",
                      details="Actuellement en 2e année."),
            Education(title="Licence - Physique", school="Université Exemple", dates="2021–2024", details="Diplôme obtenu en juin 2024."),
        ],
        languages=[Language(name="Français", level="Courant."), Language(name="Anglais", level="Niveau B2.")],
    ).ensure_ids()


def good_draft(master: Master | None = None) -> TailorDraft:
    m = master or sample_master()
    p1, p2 = m.projects[0], m.projects[1]
    return TailorDraft(
        domain="systèmes embarqués",
        summary="Étudiante en robotique, je développe des prototypes embarqués associant capteurs, actionneurs et intégration matériel-logiciel.",
        skill_groups=[
            DraftSkillGroup(label="Firmware", items=["C", "C++", "programmation embarquée"]),
            DraftSkillGroup(label="Interfaces", items=["UART", "SPI", "I2C"]),
        ],
        projects=[
            DraftProject(id=p1.id, bullets=[
                DraftBullet(label="Acquisition capteurs", text="lecture de cinq capteurs infrarouges et filtrage des mesures.",
                            source_ids=[p1.bullets[0].id]),
                DraftBullet(label="Commande d'actionneurs", text="régulation de la vitesse de deux moteurs par PWM.",
                            source_ids=[p1.bullets[1].id]),
            ]),
            DraftProject(id=p2.id, bullets=[
                DraftBullet(label="Intégration HW/SW", text="lecture d'un capteur par I2C et transmission des données par UART.",
                            source_ids=[p2.bullets[0].id, p2.bullets[1].id]),
            ]),
        ],
        education_ids=[e.id for e in m.education], language_ids=[l.id for l in m.languages],
        matched_keywords=["capteurs", "C", "UART"], gaps=["Expérience AUTOSAR", "Anglais courant"],
    )


class FakeProvider:
    """Provider double: returns canned objects keyed by schema class name."""

    name = "fake"
    enabled = True

    def __init__(self, outputs: dict[str, object] | None = None, error: str | None = None):
        self.outputs = outputs or {}
        self.error = error
        self.usage = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "calls": 0}
        self.calls: list[dict] = []

    async def structured(self, model_cls, *, system_text, user_text, max_tokens=8000):
        self.calls.append({"cls": model_cls.__name__, "system": system_text, "user": user_text})
        if self.error:
            raise LLMError(self.error)
        out = self.outputs[model_cls.__name__]
        return out(self.calls[-1]) if callable(out) else out


def fake_llm(outputs=None, error=None) -> tuple[FailoverLLM, FakeProvider]:
    prov = FakeProvider({"VerifyResult": pass_all, **(outputs or {})}, error)
    return FailoverLLM(providers=[prov]), prov
