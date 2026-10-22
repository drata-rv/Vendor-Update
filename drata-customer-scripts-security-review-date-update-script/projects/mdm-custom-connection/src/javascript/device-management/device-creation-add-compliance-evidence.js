// Import required modules
const fs = require('fs');
const csv = require('csv-parser');
const axios = require('axios');
const _ = require('lodash');
const DRATA_BASE_URL = 'https://public-api.drata.com/public';
const CSV_FILE_PATH = '/Users/hannahstarcevich/Downloads/filename.csv';
// Get connection ID from command line arguments
const connectionId = process.argv[2];

if (!connectionId) {
  console.error('Please provide a connection ID as an argument');
  process.exit(1);
}

// Function to get personnel ID by email
async function getPersonnelIdByEmail(email) {
  try {
    const response = await axios.get(`${DRATA_BASE_URL}/personnel/${email}/email`, {
      headers: {
        'Authorization': `Bearer ${process.env.DRATA_API_KEY}`
      }
    });
    return response.data.id;
  } catch (error) {
    console.error(`Error fetching personnel for ${email}:`, error.message);
    return null;
  }
}

// Function to create device in Drata
async function createDeviceInDrata(connectionId, device, personnelId) {
 const email = _.get(device, 'email', 'Unknown email');
  try {
    // Create payload with all required properties, using null as fallback
// Create payload with only non-null properties
    const payload = {
      personnelId: personnelId
    };
    
    // Only add properties with non-null values
    const deviceProps = [
      'platformName', 'platformVersion', 'serialNumber', 'antivirusEnabled',
      'autoUpdateEnabled', 'firewallEnabled', 'passwordManagerEnabled',
      'encryptionEnabled', 'screenLockEnabled', 'operatingSystem'
    ];
    
    deviceProps.forEach(prop => {
      const value = _.get(device, prop);
      if (value !== null && value !== undefined) {
        payload[prop] = value;
      }
    });

    const response = await axios.post(
      `${DRATA_BASE_URL}/custom-connections/${connectionId}/devices`,
      payload,
      {
        headers: {
          'Authorization': `Bearer ${process.env.DRATA_API_KEY}`,
          'Content-Type': 'application/json'
        }
      }
    );
    
    console.log(`Device created successfully for ${email}`);
    return response.data;
  } catch (error) {
    console.error(`Error creating device for ${email}:`, error.message);
    return null;
  }
}

// Process CSV file and create devices
async function processDevices() {
  const devices = [];
  
  // Read CSV file - TODO: add file path
  fs.createReadStream(CSV_FILE_PATH)
    .pipe(csv())
    .on('data', (data) => devices.push(data))
    .on('end', async () => {
      console.log(`Processing ${devices.length} devices...`);
      
      for (const device of devices) {
        // Get personnel ID by email
        const personnelId = await getPersonnelIdByEmail(device.email);
        
        if (!personnelId) {
          console.error(`Could not find personnel ID for ${device.email}, skipping...`);
          continue;
        }
        
        await createDeviceInDrata(connectionId, device, personnelId);
      }
      
      console.log('Device creation process completed');
    });
}

// Run the script
processDevices();

