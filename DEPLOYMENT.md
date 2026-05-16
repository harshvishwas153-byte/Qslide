# Deploying Qslide

GitHub Pages only hosts static HTML/CSS/JS. Qslide is a Flask app, so it needs a Python web service such as Render, Railway, Fly.io, PythonAnywhere, or a VPS.

## Render quick deploy

1. Push this project to GitHub.
2. In Render, create a new **Web Service** from the repository.
3. Use these settings:
   - Build command: `pip install -r requirements.txt`
   - Start command: `gunicorn app:app`
4. Add environment variables:
   - `GEMINI_API_KEY`: your Gemini API key
   - `SECRET_KEY`: a long random string
   - `GEMINI_MODEL`: optional, for example `gemini-2.5-flash`
5. Deploy.

## Oracle Cloud Always Free VM quick deploy

Use this if you want the app to keep running without Render's free sleep behavior.

### 1. Create the VM

In Oracle Cloud Console, create a compute instance:

- Type: **Virtual Machine**
- Image: **Ubuntu**
- Shape: **VM.Standard.A1.Flex**
- OCPU: `1`
- Memory: `6 GB`
- Boot volume: `50 GB`
- Public IPv4 address: enabled
- SSH key: generate or upload your key

Only continue if Oracle shows **Always Free-eligible**.

### 2. Open port 80

In the VM's virtual cloud network security list, add an ingress rule:

- Source CIDR: `0.0.0.0/0`
- IP Protocol: `TCP`
- Destination Port Range: `80`

### 3. SSH into the VM

From Windows PowerShell:

```powershell
ssh -i "C:\path\to\your-private-key.key" ubuntu@YOUR_PUBLIC_IP
```

### 4. Install Qslide

On the VM:

```bash
sudo apt-get update
sudo apt-get install -y git
git clone -b master https://github.com/harshvishwas153-byte/Qslide.git
cd Qslide
bash deploy/oracle_setup.sh
```

The script will ask for your Gemini API key and create the production service.

After it finishes, open:

```text
http://YOUR_PUBLIC_IP
```

## Important storage note

The app currently uses SQLite and local file uploads. On hosts with an ephemeral filesystem, data can disappear after restarts or redeploys unless you add persistent storage or move to a managed database.

If your host gives you a persistent disk, set:

- `DATABASE_PATH=/var/data/qslide.db`
- `UPLOAD_FOLDER=/var/data/uploads`

For a production app with real users, move from SQLite/local uploads to a hosted database and object/file storage.

## Security note

Do not commit `.env`, `qslide.db`, `uploads/`, or `__pycache__/` to GitHub. If an API key was already pushed to GitHub, rotate it in the provider dashboard and create a new one.
