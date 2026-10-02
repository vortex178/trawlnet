"""Text heuristics shared by several sources."""
from __future__ import annotations

import re

# a job-title word: used to tell titles from nav links, headings and prose
ROLE = re.compile(r"\b(engineer|developer|sde|sre|devops|architect|analyst|scientist|manager|lead|head|director|"
                  r"designer|consultant|specialist|associate|executive|officer|administrator|admin|intern|"
                  r"programmer|tester|qa|researcher|recruiter|representative|pentester|trainee)\b", re.I)
