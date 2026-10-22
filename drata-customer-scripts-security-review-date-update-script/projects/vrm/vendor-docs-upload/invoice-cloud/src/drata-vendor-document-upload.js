const fs = require('fs').promises;
const fsSync = require('fs');
const path = require('path');
const FormData = require('form-data');
const axios = require('axios');

// Enable detailed HTTP logging
process.env.NODE_DEBUG = 'http,https';

require('dotenv').config() // Configuration

const args = process.argv.slice(2);
const isVerbose = args.includes('-v') || args.includes('--verbose');

const CONFIG = {
    // DRATA_API_BASE_URL: 'https://public-api.drata.com/public',
    // DRATA_API_TOKEN: process.env.DRATA_PROD_API_TOKEN,
    DRATA_API_BASE_URL: 'http://localhost:4000/public', // Local
    DRATA_API_TOKEN: process.env.DRATA_LOCAL_API_TOKEN, // Local
    VENDOR_FOLDER_PATH: '/Users/hannahstarcevich/Desktop/test-vendors-2', // Adjust path as needed
    LOG_FILE_PATH: './drata_upload_log.txt',
    SUPPORTED_FILE_EXTENSIONS: ['.pdf', '.docx', '.odt', '.xlsx', '.ods', '.pptx', '.odp', '.gif', '.jpeg', '.jpg', '.png'],
    VERBOSE: isVerbose
};

class DrataVendorDocumentUploader {
    constructor() {
        this.logs = [];
        this.summary = {
            totalVendorsProcessed: 0,
            successfulVendors: 0,
            failedVendors: 0,
            totalDocumentsUploaded: 0,
            totalDocumentsFailed: 0,
            vendorsNotFound: 0
        };
    }

    // Logging utility
    log(message, level = 'INFO') {
        const timestamp = new Date().toISOString();
        const logEntry = `[${timestamp}] [${level}] ${message}`;
        this.logs.push(logEntry);

        // Only show verbose logs if verbose mode is enabled
          if (level === 'DEBUG' && !CONFIG.VERBOSE) {
            return;
        }
        console.log(logEntry);
    }

    // Write logs to file
    async writeLogsToFile() {
        try {
            const logContent = this.logs.join('\n') + '\n\n' + this.generateSummaryReport();
            await fs.writeFile(CONFIG.LOG_FILE_PATH, logContent, 'utf8');
            console.log(`Logs written to: ${CONFIG.LOG_FILE_PATH}`);
        } catch (error) {
            console.error('Failed to write logs to file:', error.message);
        }
    }

    // Generate summary report
    generateSummaryReport() {
        return `
=== UPLOAD SUMMARY REPORT ===
Total Vendors Processed: ${this.summary.totalVendorsProcessed}
Successful Vendors (all documents uploaded): ${this.summary.successfulVendors}
Failed Vendors (some/all documents failed): ${this.summary.failedVendors}
Vendors Not Found in Drata: ${this.summary.vendorsNotFound}
Total Documents Successfully Uploaded: ${this.summary.totalDocumentsUploaded}
Total Documents Failed to Upload: ${this.summary.totalDocumentsFailed}
================================
        `;
    }

    // Check if file extension is supported
    isSupportedFile(filename) {
        const ext = path.extname(filename).toLowerCase();
        return CONFIG.SUPPORTED_FILE_EXTENSIONS.includes(ext);
    }

    // Search for vendor by name in Drata
    async findVendorByName(vendorName) {
        try {
            this.log(`Starting vendor search for: "${vendorName}"`, 'DEBUG');
            
            // Try multiple search variations
            const searchVariations = this.generateSearchVariations(vendorName);
            this.log(`Generated ${searchVariations.length} search variations: ${JSON.stringify(searchVariations)}`, 'DEBUG');
            
            for (const searchTerm of searchVariations) {
                this.log(`Trying search variation: "${searchTerm}"`);
                
                const url = `${CONFIG.DRATA_API_BASE_URL}/vendors?q=${encodeURIComponent(searchTerm)}&limit=50`;
                this.log(`Making API request to: ${url}`, 'DEBUG');
                
                const response = await fetch(url, {
                    method: 'GET',
                    headers: {
                        'Authorization': `Bearer ${CONFIG.DRATA_API_TOKEN}`,
                        'Content-Type': 'application/json'
                    }
                });

                this.log(`API response status: ${response.status} ${response.statusText}`, 'DEBUG');

                if (!response.ok) {
                    this.log(`Search failed for "${searchTerm}": ${response.status}`, 'DEBUG');
                    continue;
                }

                const data = await response.json();
                this.log(`API returned ${data.data?.length || 0} vendors for "${searchTerm}"`, 'DEBUG');
                
                if (!data.data || data.data.length === 0) {
                    this.log(`No vendors found for search term: "${searchTerm}"`, 'DEBUG');
                    continue;
                }

                // Log vendor names returned
                this.log(`Vendors returned: ${data.data.map(v => v.name).join(', ')}`, 'DEBUG');

                // Try to find a match with this search result
                const match = this.findBestMatch(vendorName, data.data);
                if (match) {
                    this.log(`Match found using search term: "${searchTerm}"`, 'DEBUG');
                    return match;
                }
            }

            this.log(`No suitable match found for vendor: ${vendorName}`, 'WARN');
            return null;

        } catch (error) {
            this.log(`Error searching for vendor ${vendorName}: ${error.message}`, 'ERROR');
            this.log(`Error stack trace: ${error.stack}`, 'DEBUG');
            return null;
        }
    }

    // Generate multiple search variations for better matching
    generateSearchVariations(vendorName) {
        const variations = [vendorName]; // Original name first
        
        // Common company name mappings
        const nameMap = {
            'Amazon Web Services': ['AWS', 'Amazon', 'Amazon Web Services'],
            'Apple, Inc.': ['Apple', 'Apple Inc'],
            'Atlassian, Inc.': ['Atlassian', 'Atlassian Inc'],
            'Microsoft Corporation': ['Microsoft', 'MSFT'],
            'Google LLC': ['Google', 'Alphabet'],
            'Meta Platforms': ['Meta', 'Facebook'],
            'Salesforce.com': ['Salesforce'],
            'Adobe Systems': ['Adobe'],
            'Oracle Corporation': ['Oracle'],
            'International Business Machines': ['IBM'],
            'Zoom Video Communications': ['Zoom'],
            'Slack Technologies': ['Slack'],
            'Dropbox Inc': ['Dropbox'],
            'Box Inc': ['Box'],
            'HubSpot Inc': ['HubSpot'],
            'Zendesk Inc': ['Zendesk']
        };

        // Check if vendor name matches any known mappings
        for (const [key, aliases] of Object.entries(nameMap)) {
            if (vendorName.toLowerCase().includes(key.toLowerCase()) || 
                key.toLowerCase().includes(vendorName.toLowerCase())) {
                variations.push(...aliases);
            }
        }

        const normalize = (name) => name.toLowerCase().trim().replace(/[^\w\s]/g, '').replace(/\s+/g, ' ');
        const cleanName = (name) => normalize(name)
            .replace(/\b(inc|llc|ltd|corp|corporation|company|co|incorporated|systems|technologies|platforms|solutions)\b/g, '')
            .replace(/\s+/g, ' ')
            .trim();

        // Add cleaned version
        const cleaned = cleanName(vendorName);
        if (cleaned && cleaned !== normalize(vendorName)) {
            variations.push(cleaned);
        }

        // Add first word only (for cases like "Apple, Inc." -> "Apple")
        const firstWord = cleaned.split(' ')[0];
        if (firstWord && firstWord.length > 2) {
            variations.push(firstWord);
        }

        // Add acronym if multiple words
        const words = cleaned.split(' ').filter(w => w.length > 2);
        if (words.length > 1) {
            const acronym = words.map(w => w[0]).join('');
            if (acronym.length >= 2) {
                variations.push(acronym.toUpperCase());
            }
        }

        // Remove duplicates and empty strings
        return [...new Set(variations.filter(v => v && v.trim().length > 0))];
    }

    // Enhanced matching logic
    findBestMatch(originalName, vendors) {
        this.log(`Starting match analysis for "${originalName}" against ${vendors.length} vendors`, 'DEBUG');
        
        const normalize = (name) => name.toLowerCase().trim().replace(/[^\w\s]/g, '').replace(/\s+/g, ' ');
        const cleanName = (name) => normalize(name)
            .replace(/\b(inc|llc|ltd|corp|corporation|company|co|incorporated|systems|technologies|platforms|solutions)\b/g, '')
            .replace(/\s+/g, ' ')
            .trim();

        const normalizedSearchName = normalize(originalName);
        const cleanedSearchName = cleanName(originalName);
        
        this.log(`Normalized search name: "${normalizedSearchName}"`, 'DEBUG');
        this.log(`Cleaned search name: "${cleanedSearchName}"`, 'DEBUG');

        // 1. Exact match
        let exactMatch = vendors.find(vendor => 
            normalize(vendor.name) === normalizedSearchName
        );
        if (exactMatch) {
            this.log(`Found EXACT match: "${originalName}" -> "${exactMatch.name}" (ID: ${exactMatch.id})`);
            return exactMatch;
        }
        this.log(`No exact match found`, 'DEBUG');

        // 2. Clean match (ignoring suffixes)
        let cleanMatch = vendors.find(vendor => 
            cleanName(vendor.name) === cleanedSearchName
        );
        if (cleanMatch) {
            this.log(`Found CLEAN match: "${originalName}" -> "${cleanMatch.name}" (ID: ${cleanMatch.id})`);
            return cleanMatch;
        }
        this.log(`No clean match found`, 'DEBUG');

        // 3. Enhanced fuzzy matching
        this.log(`Starting fuzzy matching analysis`, 'DEBUG');
        const fuzzyMatches = vendors.map(vendor => {
            const normalizedVendorName = normalize(vendor.name);
            const cleanedVendorName = cleanName(vendor.name);
            let score = 0;
            let matchType = '';

            this.log(`Analyzing vendor: "${vendor.name}" (normalized: "${normalizedVendorName}", cleaned: "${cleanedVendorName}")`, 'DEBUG');

            // Substring matches
            if (normalizedVendorName.includes(normalizedSearchName) || normalizedSearchName.includes(normalizedVendorName)) {
                score += 25;
                matchType = 'substring match';
            }

            if (cleanedVendorName.includes(cleanedSearchName) || cleanedSearchName.includes(cleanedVendorName)) {
                score += 23;
                matchType = 'clean substring match';
            }

            // Word matching
            const searchWords = cleanedSearchName.split(' ').filter(w => w.length > 2);
            const vendorWords = cleanedVendorName.split(' ').filter(w => w.length > 2);
            
            let wordMatches = 0;
            searchWords.forEach(searchWord => {
                vendorWords.forEach(vendorWord => {
                    if (searchWord === vendorWord) {
                        score += 15;
                        wordMatches++;
                    } else if (searchWord.includes(vendorWord) || vendorWord.includes(searchWord)) {
                        score += 8;
                        wordMatches += 0.5;
                    }
                });
            });

            // Acronym matching
            if (searchWords.length > 1) {
                const searchAcronym = searchWords.map(w => w[0]).join('');
                if (normalizedVendorName.replace(/\s/g, '') === searchAcronym) {
                    score += 20;
                    matchType = 'acronym match';
                }
            }

            // First word match bonus
            if (searchWords[0] && vendorWords[0] && searchWords[0] === vendorWords[0]) {
                score += 12;
                if (!matchType) matchType = 'first word match';
            }

            return { vendor, score, matchType };
        }).filter(match => match.score >= 12) // Higher threshold
          .sort((a, b) => b.score - a.score);

        if (fuzzyMatches.length > 0) {
            const bestMatch = fuzzyMatches[0];
            this.log(`Found FUZZY MATCH: "${originalName}" -> "${bestMatch.vendor.name}" (ID: ${bestMatch.vendor.id})`, 'WARN');
            this.log(`Match details: Score=${bestMatch.score}, Type=${bestMatch.matchType}`, 'WARN');
            
            // Show top candidates
            fuzzyMatches.slice(0, 3).forEach((match, index) => {
                this.log(`  ${index + 1}. "${match.vendor.name}" (Score: ${match.score})`, 'INFO');
            });

            return bestMatch.vendor;
        }

        return null;
    }

    // Upload document to vendor using axios
    async uploadDocumentToVendor(vendorId, vendorName, filePath, filename) {
        this.log(`Starting upload for file: ${filename} to vendor: ${vendorName} (ID: ${vendorId})`, 'DEBUG');
        
        try {
            // Check if file exists
            const fileExists = await fs.access(filePath).then(() => true).catch(() => false);
            if (!fileExists) {
                this.log(`File not found: ${filePath}`, 'DEBUG');
                return { success: false, error: 'File not found' };
            }

            const fileStats = await fs.stat(filePath);
            this.log(`File stats: ${fileStats.size} bytes, modified: ${fileStats.mtime}`, 'DEBUG');

            // Read file as buffer
            const fileBuffer = await fs.readFile(filePath);
            this.log(`File buffer length: ${fileBuffer.length} bytes`, 'DEBUG');
            
            // Create FormData
            const formData = new FormData();
            formData.append('file', fileBuffer, {
                filename: filename,
                contentType: this.getContentType(filename)
            });
            // formData.append('type', 'COMPLIANCE_REPORT');

            const uploadUrl = `${CONFIG.DRATA_API_BASE_URL}/vendors/${vendorId}/documents`;
            this.log(`Upload URL: ${uploadUrl}`, 'DEBUG');
            this.log(`Content-Type: ${this.getContentType(filename)}`, 'DEBUG');
            // this.log(`Document type: COMPLIANCE_REPORT`, 'DEBUG');

            const headers = {
                'Authorization': `Bearer ${CONFIG.DRATA_API_TOKEN}`,
                ...formData.getHeaders()
            };
            
            this.log(`Request headers: ${JSON.stringify(Object.keys(headers))}`, 'DEBUG');
            this.log(`Form boundary: ${formData.getBoundary()}`, 'DEBUG');

            const response = await axios.post(uploadUrl, formData, {
                headers: headers,
                maxContentLength: Infinity,
                maxBodyLength: Infinity,
                timeout: 30000
            });

            this.log(`Upload response: ${response.status} ${response.statusText}`, 'DEBUG');
            this.log(`Upload response data: ${JSON.stringify(response.data)}`, 'DEBUG');
            this.log(`Successfully uploaded: ${filename} for vendor ${vendorName}`);
            return { success: true, data: response.data };

        } catch (error) {
            if (error.response) {
                // Server responded with error status
                this.log(`Upload failed with status: ${error.response.status}`, 'DEBUG');
                this.log(`Upload failed response headers: ${JSON.stringify(error.response.headers)}`, 'DEBUG');
                this.log(`Upload failed response body: ${JSON.stringify(error.response.data)}`, 'DEBUG');
                const errorMsg = `Failed to upload ${filename} for vendor ${vendorName}: ${error.response.status} ${error.response.statusText} - ${JSON.stringify(error.response.data)}`;
                this.log(errorMsg, 'ERROR');
                return { success: false, error: errorMsg };
            } else if (error.request) {
                // Request was made but no response received
                this.log(`Upload request failed - no response received`, 'DEBUG');
                this.log(`Upload request error: ${error.message}`, 'DEBUG');
                const errorMsg = `No response received for ${filename}: ${error.message}`;
                this.log(errorMsg, 'ERROR');
                return { success: false, error: errorMsg };
            } else {
                // Something else happened
                this.log(`Upload exception: ${error.message}`, 'DEBUG');
                this.log(`Upload exception stack: ${error.stack}`, 'DEBUG');
                const errorMsg = `Error uploading ${filename} for vendor ${vendorName}: ${error.message}`;
                this.log(errorMsg, 'ERROR');
                return { success: false, error: errorMsg };
            }
        }
    }

    // Get content type based on file extension
    getContentType(filename) {
        const ext = path.extname(filename).toLowerCase();
        const contentTypes = {
            '.pdf': 'application/pdf',
            '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
            '.odt': 'application/vnd.oasis.opendocument.text',
            '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            '.ods': 'application/vnd.oasis.opendocument.spreadsheet',
            '.pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
            '.odp': 'application/vnd.oasis.opendocument.presentation',
            '.gif': 'image/gif',
            '.jpeg': 'image/jpeg',
            '.jpg': 'image/jpeg',
            '.png': 'image/png'
        };
        return contentTypes[ext] || 'application/octet-stream';
    }

    // Process a single vendor folder
    async processVendorFolder(vendorFolderPath, vendorName) {
        this.log(`Processing vendor folder: ${vendorName}`);
        this.summary.totalVendorsProcessed++;

        // Find vendor in Drata
        const vendor = await this.findVendorByName(vendorName);
        
        if (!vendor) {
            this.log(`Vendor ${vendorName} not found in Drata - skipping`, 'WARN');
            this.summary.vendorsNotFound++;
            this.summary.failedVendors++;
            return;
        }

        // Get all files in vendor folder
        let files;
        try {
            const items = await fs.readdir(vendorFolderPath, { withFileTypes: true });
            files = items
                .filter(item => item.isFile() && this.isSupportedFile(item.name))
                .map(item => item.name);
        } catch (error) {
            this.log(`Error reading vendor folder ${vendorName}: ${error.message}`, 'ERROR');
            this.summary.failedVendors++;
            return;
        }

        if (files.length === 0) {
            this.log(`No supported files found in vendor folder: ${vendorName}`, 'WARN');
            this.summary.failedVendors++;
            return;
        }

        this.log(`Found ${files.length} files for vendor ${vendorName}`);

        // Upload each file
        let vendorSuccess = true;
        let vendorDocumentsUploaded = 0;
        let vendorDocumentsFailed = 0;

        for (const filename of files) {
            const filePath = path.join(vendorFolderPath, filename);
            const result = await this.uploadDocumentToVendor(vendor.id, vendorName, filePath, filename);
            
            if (result.success) {
                vendorDocumentsUploaded++;
                this.summary.totalDocumentsUploaded++;
            } else {
                vendorDocumentsFailed++;
                this.summary.totalDocumentsFailed++;
                vendorSuccess = false;
            }

            // Add small delay between uploads to avoid rate limiting
            await new Promise(resolve => setTimeout(resolve, 1000));
        }

        // Update vendor summary
        if (vendorSuccess && vendorDocumentsUploaded > 0) {
            this.summary.successfulVendors++;
            this.log(`Vendor ${vendorName} completed successfully: ${vendorDocumentsUploaded} documents uploaded`);
        } else {
            this.summary.failedVendors++;
            this.log(`Vendor ${vendorName} completed with issues: ${vendorDocumentsUploaded} successful, ${vendorDocumentsFailed} failed`, 'WARN');
        }
    }

    // Main execution method
    async run() {
        this.log('Starting Drata vendor document upload automation');

        // Validate configuration
        if (!CONFIG.DRATA_API_TOKEN) {
            this.log('DRATA_API_TOKEN environment variable is required', 'ERROR');
            return;
        }

        // Check if vendor folder exists
        try {
            await fs.access(CONFIG.VENDOR_FOLDER_PATH);
        } catch (error) {
            this.log(`Vendor folder not found: ${CONFIG.VENDOR_FOLDER_PATH}`, 'ERROR');
            return;
        }

        // Get all vendor folders
        let vendorFolders;
        try {
            const items = await fs.readdir(CONFIG.VENDOR_FOLDER_PATH, { withFileTypes: true });
            vendorFolders = items
                .filter(item => item.isDirectory())
                .map(item => item.name);
        } catch (error) {
            this.log(`Error reading vendor directory: ${error.message}`, 'ERROR');
            return;
        }

        this.log(`Found ${vendorFolders.length} vendor folders`);

        // Process each vendor folder
        for (const vendorName of vendorFolders) {
            const vendorFolderPath = path.join(CONFIG.VENDOR_FOLDER_PATH, vendorName);
            await this.processVendorFolder(vendorFolderPath, vendorName);
        }

        // Generate final report
        this.log('Upload automation completed');
        console.log(this.generateSummaryReport());

        // Write logs to file
        await this.writeLogsToFile();
    }
}

// Main execution
async function main() {
    const uploader = new DrataVendorDocumentUploader();

    if (CONFIG.VERBOSE) {
        uploader.log('Verbose mode enabled', 'INFO');
    }
    
    try {
        await uploader.run();
    } catch (error) {
        console.error('Fatal error:', error);
        process.exit(1);
    }
}

// Export for testing or module usage
module.exports = DrataVendorDocumentUploader;

// Run if this file is executed directly
if (require.main === module) {
    main();
}
