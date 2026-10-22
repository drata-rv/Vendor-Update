import https from 'node:https';
import { SplunkResponse } from './interfaces/SplunkResponse';
import { RequestOptions } from './interfaces/RequestOptions';
import { config } from './config/splunk';
import { Encryption } from './encryption';

export class SplunkRequest {
    private options = this.configure();

    constructor() {}

    public makeRequest() {
        const req = https.request(this.options, res => {
            console.log(`STATUS: ${res.statusCode}`);

            let data = '';

            res.on('data', (chunk: Buffer) => {
                data += chunk.toString();
            });

            res.on('end', () => {
                try {
                    const jsonResponse: SplunkResponse = JSON.parse(data);
                    console.log(JSON.stringify(jsonResponse, null, 2));
                } catch (e) {
                    console.error('Error parsing JSON:', e);
                    console.log('Raw response:', data);
                }
            });
        });

        req.on('error', (e: Error) => {
            console.error(`Problem with request: ${e.message}`);
        });

        req.end();
    }

    private configure(): RequestOptions {
        console.log(config.passwordEncrypted);
        const password = config.passwordEncrypted ? new Encryption().decrypt(config.passwordEncrypted) : config.password;
        console.log(password);
        const auth: string =
            'Basic ' + Buffer.from(`${config.username}:${password}`).toString('base64');
        return {
            hostname: config.hostname,
            port: config.port,
            path: `${config.path}?output_mode=json`,
            method: 'GET',
            headers: {
                Authorization: auth,
            },
            rejectUnauthorized: false,
        };
    }
}


