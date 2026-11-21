"""Built-in announcement templates (spec section 6.2). They load like any saved template; staff can override one by saving
a template with the same name, and can add their own. Edit the text in the builder after loading."""
from . import config

SKY, DEEP, GOLD, EMERALD, CRIMSON, VIOLET, AMBER = 0x38BDF8, 0x1E3A8A, 0xFACC15, 0x10B981, 0xDC2626, 0x7C3AED, 0xF59E0B


def _t(title, description, color, fields=None):
    return {"title": title, "description": description, "footer": config.BRAND, "color": color, "timestamp": True,
            "fields": fields or [], "thumb_id": None, "banner_id": None, "top_banner_id": None, "author": "", "buttons": []}


BUILTIN_TEMPLATES = {
    "general-sect": _t("📜 Sect Announcement", "Disciples of Blue Heaven,\n\nWrite your announcement here.", DEEP),
    "community-update": _t("🌌 Community Update", "Here is what's new in the Sect.\n\n• First update\n• Second update\n• Third update", SKY),
    "esports": _t("🎮 Esports Announcement", "Write the esports news here: roster changes, scrims, tryouts or results.", VIOLET,
                  [{"name": "Game", "value": "—", "inline": True}, {"name": "Team", "value": "—", "inline": True}]),
    "tournament-results": _t("🏆 Tournament Results", "Congratulations to everyone who competed!", GOLD,
                             [{"name": "🥇 First place", "value": "—", "inline": True},
                              {"name": "🥈 Second place", "value": "—", "inline": True},
                              {"name": "🥉 Third place", "value": "—", "inline": True}]),
    "recruitment": _t("📣 Recruitment Open", "Blue Heaven is looking for new members. Tell us what you are looking for here.", EMERALD,
                      [{"name": "Requirements", "value": "—", "inline": False},
                       {"name": "How to apply", "value": "Open a **Staff Applications** or **General Support** ticket.", "inline": False}]),
    "community-event": _t("🎉 Community Event", "Join us for a Sect event! Describe it here.", AMBER,
                          [{"name": "When", "value": "—", "inline": True}, {"name": "Where", "value": "—", "inline": True},
                           {"name": "How to join", "value": "Use the sign-up message in the events channel.", "inline": False}]),
    "realm-milestone": _t("🌌 Realm Milestone", "A great step on the path of cultivation!\n\nName the disciple or the Sect milestone here.", SKY),
    "maintenance": _t("🛠️ Maintenance Notice", "Orion and some Sect systems will be briefly unavailable.", CRIMSON,
                      [{"name": "When", "value": "—", "inline": True}, {"name": "Expected length", "value": "—", "inline": True}]),
}
