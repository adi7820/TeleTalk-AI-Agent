# Download the helper library from https://www.twilio.com/docs/python/install
import os
from twilio.rest import Client

# Set environment variables for your credentials
# Read more at http://twil.io/secure

account_sid = "AC4624de3da0a86df66b3cf49d2a22d380"
auth_token = "d08b01baed4028c238ab92696b603bb7"
client = Client(account_sid, auth_token)

call = client.calls.create(
  url="http://demo.twilio.com/docs/voice.xml",
  to="+917503966856",
  from_="+19566498425"
)

print(call.sid)