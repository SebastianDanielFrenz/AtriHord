sudo apt update
sudo apt install unzip -y

# yt-dlp needs ffmpeg to merge separate video+audio streams; without it,
# it silently falls back to old pre-muxed low quality formats (e.g. "18" = 360p)
sudo apt install ffmpeg -y

# install deno (for JS)
curl -fsSL https://deno.land/install.sh | sh
echo 'export DENO_INSTALL="$HOME/.deno"' >> ~/.bashrc
echo 'export PATH="$DENO_INSTALL/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc

# python venv and pip
sudo apt install python3-pip python3-venv -y
python3 -m venv venv
source venv/bin/activate

# python packages
python -m pip install flask textual psutil mutagen

# stop using the ksf_web_dl venv
deactivate


# pixiv util - sepearte python venv
python3 -m venv PixivUtil2/pixiv_venv
source PixivUtil2/pixiv_venv/bin/activate
python -m pip install -r PixivUtil2/requirements.txt
