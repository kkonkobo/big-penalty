"""`big-penalty portal …`: the operator's commands of the job seeker portal (DESIGN §9). Thin front end over
`jobhunt.portal.manage` and `jobhunt.portal.sync`.

Nothing secret is ever printed or logged: the pairing code and a chosen passcode are asked with a hidden prompt (never an
argument), the device token never leaves `secrets`, and a generated passcode is printed once, by `enable` / `passcode`."""

from __future__ import annotations

import socket
from datetime import UTC, datetime
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.table import Table
from rich.text import Text

from jobhunt.portal import agent, manage, protocol, secrets, settings, sync
from jobhunt.portal.client import PortalError
from jobhunt.portal.manage import ManageError

portal_app = typer.Typer(
    help="Portail de la personne accompagnée : appairage, activation par profil, validation, synchronisation (toutes les 5 minutes "
         "quand Big Penalty est ouvert).", no_args_is_help=True)
agent_app = typer.Typer(help="État et retrait de l’ancien agent launchd (macOS).",
                        no_args_is_help=True)
portal_app.add_typer(agent_app, name="agent")
console = Console()

TokenStoreOpt = Annotated[str, typer.Option("--token-store", help="Où garder le jeton de l'appareil : keyring (trousseau macOS) ou file")]
PassFlag = Annotated[bool, typer.Option("--passcode", help="Choisir le code vous-même (saisie masquée) au lieu d'en générer un")]
EVENT_KIND_LABELS = {"answer": "Réponse", "question_answer": "Réponse à une question", "custom_edit": "Réponse personnalisée modifiée",
                     "custom_delete": "Réponse personnalisée supprimée", "intent": "Envoi de candidature", "cancel": "Annulation"}
STATE_LABELS = {"received": "reçu", "awaiting_review": "à valider", "queued": "en file", "running": "en cours", "applied": "appliqué",
                "conflict": "conflit", "rejected": "refusé", "done": "terminé", "refused": "refusé", "failed": "échec",
                "cancelled": "annulé", "interrupted": "interrompu", "lost": "perdu"}
MASKED = "(valeur masquée)"


def _p(text: str = "") -> None:
    console.print(text, markup=False, highlight=False, soft_wrap=True)


def _fail(msg: str) -> typer.Exit:
    _p(f"Erreur : {msg}")
    return typer.Exit(1)


def _call(fn, *args, **kw):
    """Run a portal action; its French errors become a short message and exit code 1 (no traceback)."""
    try:
        return fn(*args, **kw)
    except (ManageError, PortalError, secrets.PortalSecretError, agent.AgentError) as e:
        raise _fail(str(e)) from None


def ago(dt: datetime | None) -> str:
    if dt is None:
        return "jamais"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    s = (datetime.now(UTC) - dt).total_seconds()
    if s < 60:
        return "à l'instant"
    if s < 3600:
        return f"il y a {int(s // 60)} min"
    if s < 86400:
        return f"il y a {int(s // 3600)} h"
    return f"il y a {int(s // 86400)} j"


def mask_event_value(ev, text: str) -> str:
    """The same rule as the rest of the portal: answers to write-only topics are never shown, not even on this Mac."""
    if not text:
        return text
    path = str(ev.payload.get("path", "")) if ev.kind == "answer" else ""
    if (path and path in settings.get_portal_settings().write_only) or settings.is_masked_question(ev.label):
        return MASKED
    return text


def change_text(ev) -> str:
    before, after = mask_event_value(ev, ev.before_text), mask_event_value(ev, ev.after_text)
    if before == after == MASKED:
        return MASKED
    if before and after:
        return f"{before} → {after}"
    return after or before


def print_credentials(creds: manage.Credentials, chosen: bool) -> None:
    _p("")
    _p("=" * 60)
    _p(f"Adresse     : {creds.address}")
    _p(f"Identifiant : {creds.handle_shown}")
    _p("Code        : celui que vous avez saisi" if chosen else f"Code        : {creds.passcode_shown}")
    _p("=" * 60)
    _p("Transmettez l'identifiant et le code par deux canaux différents. Le code n'est pas enregistré : notez-le maintenant.")


def _ask_passcode() -> str:
    return typer.prompt("Code choisi (10 caractères au moins, saisie masquée)", hide_input=True, confirmation_prompt=True)


# ---------------------------------------------------------------- pairing
@portal_app.command("pair")
def portal_pair(
    url: Annotated[str, typer.Argument(help="Adresse HTTPS configurée pour le portail")],
    token_store: TokenStoreOpt = "keyring",
    name: Annotated[Optional[str], typer.Option("--name", help="Nom de cet appareil (défaut : le nom de la machine)")] = None,
) -> None:
    """Appaire ce Mac au portail (une seule fois). Le code d'appairage est demandé en saisie masquée."""
    code = typer.prompt("Code d'appairage", hide_input=True)
    info = _call(manage.pair, url, code, name or socket.gethostname() or "Mac", token_store)
    _p(f"Appairé avec {info.origin}{info.base_path} (appareil « {info.device_name} », jeton gardé dans : {info.token_store}).")
    _p("Maintenant, retirez PORTAL_PAIRING_CODE et PORTAL_PAIRING_EXPIRES du serveur et redéployez-le : "
       "le code d'appairage ne doit pas rester actif.")


@portal_app.command("recover")
def portal_recover(url: Annotated[str, typer.Argument(help="Adresse du portail")], token_store: TokenStoreOpt = "keyring") -> None:
    """Après la perte de data/portal/ : récupère l'appairage depuis le jeton resté dans le trousseau et liste les profils du serveur.

    Limites : le serveur ne sait pas à quel profil local correspond chaque clé, et ni les identifiants ni les codes ne
    peuvent être retrouvés. Il faut réactiver chaque profil local (enable --profile-key CLÉ pour réutiliser une clé listée) ;
    les validations en attente sont perdues et à redonner. Sans jeton (trousseau perdu), il faut appairer de nouveau."""
    keys = _call(manage.recover, url, token_store)
    _p(f"Appairage rétabli avec {url}.")
    if not keys:
        _p("Le serveur ne connaît aucun profil de cet appareil : utilisez « big-penalty portal enable PROFIL_ID ».")
        return
    _p("Profils connus du serveur pour cet appareil :")
    for k in keys:
        held = " · suspendu par l'administrateur" if k.held else ""
        _p(f"  {k.profile_key} · {'activé' if k.enabled else 'désactivé'}{held}")
    _p("Pour chaque profil local, réactivez-le avec une clé listée (nouvel identifiant et nouveau code) :")
    _p(f"  big-penalty portal enable PROFIL_ID --profile-key {keys[0].profile_key}")
    _p("Une clé que vous n'utilisez pas reste sur le serveur.")


@portal_app.command("unpair")
def portal_unpair(force: Annotated[bool, typer.Option("--force", help="Désactiver d'abord, sur ce Mac, les profils encore activés")] = False) -> None:
    """Oublie l'appairage de ce Mac (supprime le jeton local)."""
    _call(manage.unpair, force)
    _p("Appairage supprimé sur ce Mac.")


# ---------------------------------------------------------------- profiles
@portal_app.command("enable")
def portal_enable(
    profile_id: Annotated[int, typer.Argument(help="Numéro du profil")],
    passcode: PassFlag = False,
    review: Annotated[bool, typer.Option("--review", help="Mode validation : vous validez chaque action avant qu'elle soit appliquée")] = False,
    profile_key: Annotated[Optional[str], typer.Option("--profile-key", help="Réutiliser une clé de profil du serveur (après « recover »)")] = None,
) -> None:
    """Active le portail pour un profil et affiche une seule fois l'adresse, l'identifiant et le code."""
    chosen = _ask_passcode() if passcode else None
    creds = _call(manage.enable, profile_id, chosen, review, profile_key=profile_key)
    _p(f"Portail activé pour le profil {profile_id} (mode {'validation' if review else 'direct'}).")
    print_credentials(creds, chosen is not None)


@portal_app.command("passcode")
def portal_passcode(profile_id: Annotated[int, typer.Argument(help="Numéro du profil")], passcode: PassFlag = False) -> None:
    """Nouveau code : coupe les sessions et les appareils connus, annule les validations en attente."""
    chosen = _ask_passcode() if passcode else None
    creds = _call(manage.rotate_passcode, profile_id, chosen)
    _p(f"Nouveau code pour le profil {profile_id}. Les appareils connectés sont déconnectés.")
    print_credentials(creds, chosen is not None)


@portal_app.command("revoke")
def portal_revoke(profile_id: Annotated[int, typer.Argument(help="Numéro du profil")]) -> None:
    """Déconnecte tous les appareils du profil, sans changer le code."""
    _call(manage.revoke, profile_id)
    _p(f"Sessions du profil {profile_id} coupées : le code reste valable.")


@portal_app.command("mode")
def portal_mode(profile_id: Annotated[int, typer.Argument(help="Numéro du profil")],
                mode: Annotated[str, typer.Argument(help="direct ou review")]) -> None:
    """Change le mode : direct (les réponses s'appliquent seules) ou review (vous validez chaque action)."""
    _call(manage.set_mode, profile_id, mode)
    _p(f"Profil {profile_id} : mode {'validation' if mode == 'review' else 'direct'}.")


@portal_app.command("review")
def portal_review(
    profile_id: Annotated[int, typer.Argument(help="Numéro du profil")],
    apply_all: Annotated[bool, typer.Option("--apply-all", help="Appliquer toutes les actions en attente")] = False,
    reject_all: Annotated[bool, typer.Option("--reject-all", help="Refuser toutes les actions en attente")] = False,
) -> None:
    """Sans option : liste les actions en attente de validation. Avec --apply-all ou --reject-all : les traite toutes."""
    if apply_all and reject_all:
        raise typer.BadParameter("--apply-all et --reject-all s'excluent l'un l'autre.")
    if apply_all:
        rep = _call(manage.review_apply, profile_id)
        if rep.skipped_reason:
            raise _fail(f"rien n'a été appliqué ({rep.skipped_reason}).")
        _p(f"Appliqué : {_states(rep.states)}.")
    elif reject_all:
        n = _call(manage.review_reject, profile_id)
        _p(f"{n} action(s) refusée(s).")
    else:
        events = _call(manage.review_list, profile_id)
        if not events:
            _p("Aucune action en attente de validation.")
            return
        for ev in events:
            _p(f"{ev.event_id[:8]}  {EVENT_KIND_LABELS.get(ev.kind, ev.kind)}  {ev.label}  {change_text(ev)}".rstrip())
        _p(f"{len(events)} action(s) en attente : « portal review {profile_id} --apply-all » ou « --reject-all ».")


def _states(states: dict[str, int]) -> str:
    return ", ".join(f"{n} {STATE_LABELS.get(s, s)}" for s, n in sorted(states.items())) or "rien"


@portal_app.command("disable")
def portal_disable(
    profile_id: Annotated[int, typer.Argument(help="Numéro du profil")],
    purge: Annotated[bool, typer.Option("--purge", help="Effacer aussi les données du profil sur le serveur")] = False,
) -> None:
    """Désactive le portail pour un profil (d'abord sur ce Mac, puis sur le serveur)."""
    res = _call(manage.disable, profile_id, purge)
    _p(f"Profil {profile_id} désactivé sur ce Mac.")
    if res.server_updated:
        _p("Serveur mis à jour" + (" : données effacées." if purge else "."))
        return
    _p(f"Le serveur n'a pas pu être joint ou a refusé la demande : {res.error}")
    _p("Il n'a pas été mis à jour : l'accès du profil reste ouvert côté serveur. Une fois le serveur joignable, relancez "
       "« portal enable » puis « portal disable » pour le couper.")
    raise typer.Exit(1)


@portal_app.command("pause")
def portal_pause() -> None:
    """Interrupteur local : plus rien n'est appliqué ni envoyé depuis le portail tant que c'est en pause."""
    _call(manage.pause)
    _p("Portail en pause sur ce Mac.")


@portal_app.command("resume")
def portal_resume() -> None:
    """Lève la pause locale."""
    _call(manage.resume)
    _p("Portail repris sur ce Mac.")


@portal_app.command("resync")
def portal_resync(profile_id: Annotated[int, typer.Argument(help="Numéro du profil")],
                  yes: Annotated[bool, typer.Option("--yes", "-y", help="Ne pas demander confirmation")] = False) -> None:
    """Nouveau flux d'événements (après une restauration du serveur ou une erreur de séquence)."""
    if not yes and not typer.confirm("Un nouveau flux abandonne les actions du portail encore en attente pour ce profil. Continuer ?"):
        _p("Annulé.")
        raise typer.Exit(1)
    _call(manage.resync, profile_id)
    _p(f"Nouveau flux pour le profil {profile_id} : les actions en attente ont été abandonnées.")


# ---------------------------------------------------------------- sync
def report_line(r: sync.ProfileSyncReport) -> str:
    who = f"profil {r.profile_id}" if r.profile_id is not None else "synchronisation"
    if r.error:
        return f"{who} : erreur : {r.error}"
    parts = [f"{r.pulled} reçu(s)"]
    if r.applied is not None:
        parts.append("appliqué : " + _states(r.applied.states))
    parts.append("état envoyé au serveur" if r.pushed else "rien envoyé au serveur")
    if r.uploaded:
        parts.append(f"{r.uploaded} document(s) transmis")
    parts.append(f"{r.executed} demande(s) d’approbation traitée(s)")
    if r.message:
        parts.append(r.message)
    return f"{who} : " + " · ".join(parts)


@portal_app.command("sync")
def portal_sync(
    profile_ids: Annotated[Optional[list[int]], typer.Argument(help="Profils à synchroniser (défaut : tous les profils activés)")] = None,
    no_execute: Annotated[bool, typer.Option("--no-execute", help="Laisser les demandes d’approbation en attente")] = False,
) -> None:
    """Récupère les demandes, enregistre les accords, puis publie l’état. Aucun envoi pendant la synchronisation."""
    reports = _call(sync.sync_all, list(profile_ids) if profile_ids else None, not no_execute)
    if not reports:
        _p("Aucun profil activé à synchroniser.")
        return
    failed = False
    for r in reports:
        _p(report_line(r))
        if r.error and r.error != sync.BUSY:
            failed = True
    if failed:
        raise typer.Exit(1)


# ---------------------------------------------------------------- status
@portal_app.command("status")
def portal_status(tail: Annotated[int, typer.Option("--tail", help="Afficher les N derniers événements du journal de chaque profil")] = 0) -> None:
    """État du portail : appairage, pause et, par profil, activation, mode, identifiant, dernière synchronisation, compteurs."""
    st = _call(manage.status)
    if not st.paired:
        _p("Cet appareil n'est pas appairé : lancez « big-penalty portal pair ADRESSE ».")
        return
    _p(f"Portail : {st.origin}{st.base_path} · appareil : {st.device_name or 'inconnu'} · "
       f"{'EN PAUSE' if st.paused else 'actif'}")
    if not st.profiles:
        _p("Aucun profil activé : « big-penalty portal enable PROFIL_ID ».")
        return
    table = Table("Profil", "Activé", "Mode", "Identifiant", "Dernière synchro", "Dernière erreur", "Compteurs")
    for p in st.profiles:
        counts = f"{p.pending} reçu(s) · {p.awaiting_review} à valider · {p.queued} en file · {p.sends_today} envoi(s) aujourd'hui"
        table.add_row(Text(f"{p.profile_id} {p.name}".strip()), Text("oui" if p.enabled else "non") + Text(" (suspendu)" if p.held else ""),
                      Text("validation" if p.mode == "review" else "direct"),
                      Text(protocol.grouped(p.handle, 5) if p.handle else "—"), Text(ago(p.last_sync_at)), Text(p.last_error or "—"),
                      Text(counts))
    console.print(table)
    if tail > 0:
        for p in st.profiles:
            _p(f"Journal du profil {p.profile_id} :")
            for ev in _call(manage.journal_tail, p.profile_id, tail):
                _p(f"  {ev.at:%d/%m %H:%M}  {EVENT_KIND_LABELS.get(ev.kind, ev.kind)}  {STATE_LABELS.get(ev.state, ev.state)}  "
                   f"{ev.label}  {change_text(ev)}".rstrip())


# ---------------------------------------------------------------- launchd agent
@agent_app.command("install")
def agent_install() -> None:
    """La synchronisation est intégrée ; aucun agent n’est installé."""
    _p("Synchronisation intégrée toutes les 5 minutes quand Big Penalty est ouvert. Aucun agent launchd installé.")
    _p("Pour retirer un ancien agent : big-penalty portal agent uninstall")


@agent_app.command("uninstall")
def agent_uninstall() -> None:
    """Décharge l'agent launchd et supprime son fichier."""
    _p("Agent désinstallé." if _call(agent.uninstall) else "Rien à désinstaller.")


@agent_app.command("status")
def agent_status() -> None:
    """État de l’ancien agent launchd et de son journal."""
    st = _call(agent.status)
    _p(f"Fichier plist : {'présent' if st.installed else 'absent'} · agent : {'chargé' if st.loaded else 'non chargé'}")
    if st.interval_s:
        _p(f"Intervalle : {st.interval_s // 60} min")
    if st.log_path:
        _p(f"Dernières lignes de {st.log_path.name} :")
        for line in st.log_tail or []:
            _p("  " + line)
    else:
        _p("Aucun journal pour l'instant.")
