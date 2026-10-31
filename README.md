# ARGUS — Financial Intelligence Demo

ARGUS is an offline-first financial intelligence demo for exploring transaction activity, reviewing alerts, and understanding how accounts connect through money transfers. It brings transaction monitoring, account profiles, transfer maps, investigations, and report generation into one Python application.

ARGUS is built for learning, demonstrations, and project presentations. It uses fictional demo profiles and case records; it is not connected to real bank, police, court, employment, or government systems.

## What ARGUS includes

- **Overview** — See account, transaction, and alert summaries, recent activity, and accounts selected for review.
- **Live Transfers** — Run a transaction simulation and view alerts raised during the current session.
- **Account Search** — Find an account and review available transaction and profile details.
- **Transfer Map** — Explore incoming and outgoing money flows around a selected account, with connected accounts and transfer direction.
- **Detection Alerts** — Review alerts by account, see the reasons for review, and inspect incoming and outgoing transfers.
- **Investigations** — Follow a selected account through its transfer branches. ARGUS also checks connected accounts for available profiles, shared transfer contacts, listed relationships, and fictional case context.
- **Entity Intelligence** — Search the generated demo profiles, relationship records, and case records.
- **Reports** — Review investigation findings and download reports, including CSV data.

## How an investigation works

Choose an account from the live simulation or saved historical alerts. ARGUS examines transfers around that account in both directions, follows connected transfer branches up to the selected depth, and summarizes the accounts reached.

It then displays available profile and relationship information for the accounts in scope. The investigation view is designed to explain what was checked and why an alert may need review. A business or profession may provide context for transaction activity, but it does not automatically clear or condemn an account.

## Technology

- Python
- Streamlit
- SQLite
- Pandas
- Plotly

## Run locally

### 1. Get the project

Clone your repository and open its folder:

```powershell
git clone <your-repository-url>
cd <your-project-folder>
```

### 2. Create and activate a virtual environment

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
```

If PowerShell does not allow activation, you can run the environment’s Python directly or follow the Python installation guidance for your system.

### 3. Install the dependencies

If the repository contains `requirements.txt`, install its listed packages:

```powershell
py -m pip install -r requirements.txt
```

### 4. Start ARGUS

```powershell
streamlit run app.py
```

Streamlit will show a local address in the terminal. Open that address in your browser.

## Optional: generate entity demo data

If the project includes `generate_entity_data.py`, run it from the project folder to add fictional entity profiles, relationships, and case records:

```powershell
py generate_entity_data.py
```

This step can take some time when generating data for a large account set. It is optional if you only want to run the app with the database already provided. The generator creates fictional demo context; it does not verify real people or organizations.

## Project files

The repository may include files such as:

```text
app.py
database.py
detection_engine.py
pattern_catalog.py
pattern_lab.py
transfer_map.py
generate_entity_data.py
assests/
  style.css
data/
  argus.db
requirements.txt
README.md
```

The project has historically used the folder name `assests`. Keep that spelling if the app expects it, or update the code and folder together.

## Demo data and responsible use

All generated names, occupations, businesses, relationships, and case records are fictional. A case entry is not a verified legal record, and a missing entry does not mean an account has been cleared. ARGUS does not perform external background checks.

An alert is a signal for human review, not proof of fraud, money laundering, terrorism financing, or other wrongdoing. The results depend on the local transaction data and the detection logic included in the project.

Do not upload private data, credentials, `.env` files, or a database containing information that should not be public. For a public GitHub repository, use fictional sample data and keep local environments, caches, logs, and generated exports out of version control.

## Screenshots

<img width="1917" height="970" alt="image" src="https://github.com/user-attachments/assets/ef1eb5f2-4882-44be-a3db-1b1284479055" />
<img width="1902" height="827" alt="image" src="https://github.com/user-attachments/assets/f4c7518b-9582-4592-8c3f-794d29ddb9d6" />
<img width="1891" height="900" alt="image" src="https://github.com/user-attachments/assets/a6ea6537-9112-47ca-ab73-c3ce6e676a3e" />
<img width="1915" height="911" alt="image" src="https://github.com/user-attachments/assets/fcfc0bbe-b51b-4738-a532-6f7657d739b7" />
i have attached the picture of few pages, the first two are from dashboard and the other two are from final report of this project

```markdown
![ARGUS overview](screenshots/overview.png)
```

## Project status

ARGUS is a demonstration and development project. Its detection results and generated entity context are intended to make transaction patterns easier to explore; they are not a substitute for validated data, independent checks, or professional review.
