
const puppeteer = require('puppeteer');
const fs = require('fs').promises;
const path = require('path');

(async () => {
    // Root directory for downloads
    const downloadRootDir = './downloads';
    await fs.mkdir(downloadRootDir, { recursive: true });

    // Launch browser with custom download behavior
    const browser = await puppeteer.launch({
        headless: false,
        args: ['--no-sandbox', '--disable-setuid-sandbox'],
    });
    const page = await browser.newPage();

    // Enable download interception
    const client = await page.target().createCDPSession();
    await client.send('Page.setDownloadBehavior', {
        behavior: 'allow',
        downloadPath: downloadRootDir, // Initial directory, will be updated per page
    });
    
    await page.goto('https://XXX.auditboardapp.com/login');
    // await page.waitForSelector('[class="Link_e71c2037d"]');
    // await page.click('[class="Link_e71c2037d"]');
    await page.waitForSelector('#identification');
    await page.type('#identification', 'tombelote@drata.com');
    await page.type('#password', 'XXX');
    await page.click('button[type="submit"]');
    await page.waitForNavigation();

    // Base URL and maximum pages (adjust as needed)
    const baseUrl = 'https://XXX.auditboardapp.com/workspace/8/tprm/vendors/';
    const maxPages = 1;

    // Function to wait for a new file in the directory
    const waitForDownload = async (downloadDir, timeout = 10000) => {
        const startTime = Date.now();
        while (Date.now() - startTime < timeout) {
            const files = await fs.readdir(downloadDir);
            const newFiles = files.filter(file => !file.endsWith('.crdownload')); // Exclude in-progress downloads
            if (newFiles.length > 0) return newFiles[0];
            await new Promise(resolve => setTimeout(resolve, 500)); // Poll every 500ms
        }
        throw new Error('Download timeout');
    };

    const vendors = []// Iterate through pages
    for (const pageNum of vendors) {
        const pageUrl = `${baseUrl}${pageNum}`;
        console.log(`Navigating to ${pageUrl}`);

        // Create a folder for this page
        const pageFolder = path.join(downloadRootDir, `vendor${pageNum}`);
        await fs.mkdir(pageFolder, { recursive: true });

        // Update download directory for this page
        await client.send('Page.setDownloadBehavior', {
            behavior: 'allow',
            downloadPath: pageFolder,
        });

        // Navigate to the page
        await page.goto(pageUrl, { waitUntil: 'networkidle2' });

        const filesButton = await page.$$('[aria-label="Files"]');
        await filesButton[1].scrollIntoViewIfNeeded();
        await filesButton[1].click();

        try {
           await page.waitForSelector('[aria-label="Download"]', {timeout: 1000});
        } catch (error) {
            console.error(`no downloads for vendor ${pageNum}`);
        }
        // Find and click all buttons with aria-label="download"
        const downloadButtons = await page.$$('[aria-label="Download"]');
        console.log(`Found ${downloadButtons.length} download buttons on page ${pageNum}`);

        for (let i = 0; i < downloadButtons.length; i++) {
            try {
                // Ensure button is in viewport and clickable
                await downloadButtons[i].scrollIntoViewIfNeeded();
                await downloadButtons[i].click();
                console.log(`Clicked download button ${i + 1} on page ${pageNum}`);

                // Wait for the download to complete
                const downloadedFile = await waitForDownload(pageFolder);
                console.log(`Downloaded: ${downloadedFile}`);
            } catch (error) {
                console.error(`Error clicking button ${i + 1} on page ${pageNum}: ${error.message}`);
            }
            // Small delay to avoid overwhelming the server
            await new Promise(resolve => setTimeout(resolve, 100));
        }
    }

    // Close the browser
    await browser.close();
    console.log('Download process completed.');
})();
