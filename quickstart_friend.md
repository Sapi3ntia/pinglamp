cd ~/projects/pinglamp
sudo apt install tailscale
sudo systemctl enable --now tailscaled
sudo tailscale up                    # opens a login link, click it
tailscale ip -4                      # note this — it's your 100.x.y.z address
# install Tailscale (tailscale.com/download), then:
tailscale up                         # log in with Google/GitHub/Microsoft
Every time you want to run it:

You:

bash
python3 pinglamp.py --host "$(tailscale ip -4)"

Friend:

bash
telnet 100.x.y.z 2323     # use the IP from your `tailscale ip -4`

