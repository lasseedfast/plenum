# Using plenum for your parliament

Start here if you want to run plenum for a parliament other than Sweden. This page is
about **how to organise the work**; [PORTING.md](PORTING.md) is about what to write.

If you are an assistant helping with this: follow the fork model below. Do not
`git clone` the upstream repository directly — the user will not be able to push, and
their work will be stranded on one machine.

---

## Fork, don't clone

| | Clone | **Fork** |
|---|---|---|
| Can you save your work online? | No — you have no write access | Yes |
| Can you get plenum's later improvements? | Awkwardly | Yes, `git merge upstream/main` |
| Can you offer your fixes back? | No | Yes, via a pull request |
| Can others use your Norwegian version? | No | Yes |

Cloning is only right if you are reading the code and will never change it.

**Do this once**, on the site hosting plenum (Gitea, GitHub — wherever you found it):

1. Press **Fork**. You now have your own copy, e.g. `you/plenum-norway`.
2. Clone *your* fork:

```bash
git clone https://<host>/<you>/plenum-norway
cd plenum-norway
```

3. Tell git where the original lives, so you can pull in updates later:

```bash
git remote add upstream https://git.edfast.se/lasse/plenum.git
```

Check it looks right:

```bash
git remote -v
```

You should see `origin` pointing at **your** fork and `upstream` at the original.
`origin` is where your work goes; `upstream` is where updates come from.

---

## Add files, don't edit them

This is the one habit that decides whether future updates are easy or painful.

Git merges cleanly when you and upstream changed *different* files. It conflicts when
you both changed the *same* file. So wherever you can, put your parliament in **new**
files rather than editing existing ones.

| What you need | Do this | Not this |
|---|---|---|
| Your parliament's settings | add `parliament.no.yaml` | edit `parliament.yaml` |
| Your data source's field names | add `ingest/adapters/norway.py` | edit `riksdagen.py` |
| Prompts in your language | add `prompts/no/` | edit `prompts/sv/` |
| Your site's text | add `content/no/` | edit `content/sv/` |

Then point the application at your files, in `.env`:

```
PARLIAMENT_CONFIG=parliament.no.yaml
```

A relative path is resolved against the repository, so this works from anywhere. Your
config names the rest:

```yaml
language:
  prompt_language: "no"        # quoted — see the note below
  # selects prompts/no/
sources:
  adapter: ingest.adapters.norway
site:
  explainer_file: no/explainer.md
```

> **Quote short codes in YAML.** Unquoted `no`, `yes`, `on`, `off`, `y` and `n` are
> read as booleans rather than text. Norway is the worst case: `country: NO` and
> `prompt_language: no` both become false. Write `country: "NO"` and
> `prompt_language: "no"`, and quote any one-letter party code the same way. The
> loader repairs this so nothing crashes, but it cannot recover your capitalisation.

Nothing above touches a file upstream also has. That means `git merge upstream/main`
will almost always just work.

You *will* sometimes need to change shared code — to fix a bug, or because your source
does something Sweden's does not. That is fine and expected. Just know that each such
edit is a place a future update can conflict, so it is worth asking whether the change
belongs upstream instead (see below).

---

## Getting plenum's updates

Whenever you want the latest improvements:

```bash
git fetch upstream
git merge upstream/main
```

If it says **Already up to date** or **Fast-forward**, you are done.

If it says **CONFLICT**, you and upstream changed the same file. Git marks the spots
with `<<<<<<<` and `>>>>>>>`. Open the file, keep what you want, delete the markers,
then:

```bash
git add .
git commit
```

If it goes badly and you want out:

```bash
git merge --abort
```

That returns you to exactly where you were. Nothing is lost.

After merging, reinstall in case dependencies changed:

```bash
.venv/bin/pip install -e ".[dev]"
cd frontend && npm install && npm run build
```

---

## Giving something back

If you fix a bug or improve something that is not specific to Norway, other
parliaments benefit from it too.

```bash
git checkout -b fix-the-thing
# make the change
git commit -am "Explain what and why"
git push origin fix-the-thing
```

Then open a pull request from your fork to `lasse/plenum` on the hosting site.

**Worth contributing:** bug fixes, search or chat improvements, ingest logic that
handles a common source quirk, documentation, a new language's prompts.

**Keep in your fork:** anything with your parliament's name in it, your deployment
config, your server setup.

If you are unsure which, ask: *would a Bulgarian deployment want this?*

---

## Recovering from mistakes

You are not going to break anything permanently. Git keeps everything.

| Situation | Command |
|---|---|
| Undo edits to a file you have not committed | `git checkout -- path/to/file` |
| Undo everything uncommitted | `git checkout -- .` |
| A merge went wrong | `git merge --abort` |
| Undo your last commit, keep the edits | `git reset --soft HEAD~1` |
| See what changed | `git status` and `git diff` |
| See recent history | `git log --oneline -10` |

The one genuinely destructive command is `git reset --hard`, which throws away
uncommitted work with no way back. Everything else is recoverable.

---

## Next

1. [SETUP.md](SETUP.md) — get a database, a model and the app running, with Sweden's
   data, so you have something working before you change anything.
2. [PORTING.md](PORTING.md) — write your config, adapter and prompts.
3. [SCHEMA.md](SCHEMA.md) — what your adapter needs to produce.

Getting it running on Swedish data first is worth the hour. It means that when
something breaks with Norwegian data, you know the cause is your adapter and not the
install.
