# Install  Splunk Enterprise
splunk-9.4.2-e9664af3d956-darwin-intel.tgz (https://www.splunk.com/en_us/download/splunk-enterprise.html)
export SPLUNK_HOME=<splunk folder>/splunk

# Start the Splunk Service
`cd splunk/bin`
`./splunk start`
By default the server runs at on port 8089.

# Test the Splunk Server
`curl -u ${USERNAME}:${PASSWORD} https://localhost:8089/services/data/indexes/splunklogger`

# Build the code dependencies
`npm install`

# Run the code
`npx ts-node src/main.ts`