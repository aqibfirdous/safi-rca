# The two demo repositories are separate git repositories, not folders of this one.

Each is a real repository with its own history, and each test scenario is pinned to one
exact commit of it:

| repository    | branch | commit                                                      | state                                        |
| ------------- | ------ | ----------------------------------------------------------- | -------------------------------------------- |
| sample-repo   | main   | `467b3d80a3a2c7bd0cecd81e44838bb3ff5fdc0c` (`467b3d8`)     | defect: tier lookup has no default           |
| sample-repo   | main   | `ad23862538978a8f1e39a251c6882e9b1d29839a` (`ad23862`)     | fallback added; a Decimal-scale defect remains |
| alt-repo      | main   | `f3ab813dcd44e258451682b0da624b4840d56092` (`f3ab813`)     | defect: ring-buffer drain order after wrap    |

`safi-rca` analyses them **read-only**, so their working trees stay clean and `HEAD` never
moves. The shas above are asserted at test session start; if a fixture is ever rewritten,
the acceptance suite fails rather than silently testing something else.

They are registered as submodules pointing at the bare mirrors in `fixtures/origin/`, so
a fresh clone of this repository can restore them:

```bat
git submodule update --init
```

The mirrors exist only so the fixtures are self-contained offline; nothing is fetched
from a network.
