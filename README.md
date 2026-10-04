# Mercari Japan new-listing alerts

Checks your Mercari searches every ~10 minutes on GitHub's servers (free, your
computer can be off) and emails you the name, price and link of any new listing.

## Files
| File | Purpose |
|---|---|
| `searches.txt` | **Your keywords, one per line.** Add a line to add a search. |
| `monitor.py` | The checker/emailer. |
| `requirements.txt` | Python dependency (Playwright). |
| `.github/workflows/mercari.yml` | Schedule (every 10 min). |
| `seen.json` | Created automatically; remembers listings already reported. |

## One-time setup (about 10 minutes)

### 1. Make a Gmail app password
The script sends the email through Gmail. Using `roy.yn.zhang@gmail.com` as the
sender too is simplest (alerts arrive "from yourself").
1. Turn on 2-Step Verification: https://myaccount.google.com/security
2. Create an app password: https://myaccount.google.com/apppasswords
   (name it "mercari"). Copy the 16-character code.

### 2. Create the GitHub repository
1. Sign in at github.com (free account) → **New repository**.
2. Name it e.g. `mercari-monitor`. Choose **Public** (see note below). Create it.
3. Upload `monitor.py`, `searches.txt`, `requirements.txt`, `README.md`
   (**Add file → Upload files**).
4. Create the workflow: **Add file → Create new file**, type the name
   `.github/workflows/mercari.yml` (typing the slashes creates the folders),
   paste in the contents of `mercari.yml`, and commit.

### 3. Add your secrets
Repo → **Settings → Secrets and variables → Actions → New repository secret**:
- `GMAIL_USER` = `roy.yn.zhang@gmail.com`
- `GMAIL_APP_PASSWORD` = the 16-character code
- (optional) `ALERT_TO` = where to send alerts; defaults to `roy.yn.zhang@gmail.com`

Also check **Settings → Actions → General → Workflow permissions** is set to
*Read and write permissions*.

### 4. Test it
Repo → **Actions → Mercari monitor → Run workflow**:
1. Tick **test_email** and run. You should receive a test email within a minute.
2. Run again with it unticked. The first run only records what is currently listed
   (no email). From then on, new listings trigger an email.

## Adding / removing searches
Edit `searches.txt` on GitHub (pencil icon), add a line such as `旧アジア 切手`,
commit. The next run records a baseline for it silently, then alerts on new items.
Lines starting with `#` are ignored.

## Notes
- **Cost:** public repos get unlimited free Actions minutes. A *private* repo only
  gets 2,000 free minutes/month, which a 10-minute schedule would exceed; if you
  want it private, change the cron line to `"0 * * * *"` (hourly).
  Public means anyone can see your keywords, not your password or email secrets.
- **Timing:** GitHub schedules can slip by several minutes at busy times.
- **If Mercari blocks or changes its page:** the run fails and GitHub emails you a
  "workflow failed" notice; the `debug` artifact on that run has a screenshot.
- GitHub pauses scheduled workflows in repos with no activity for 60 days; the
  state commits usually prevent that, but if it happens, re-enable it in the Actions tab.
