import { SplunkRequest } from './splunk-request';
import { Encryption } from './encryption';

function main() {
    const splunkRequest = new SplunkRequest();
    splunkRequest.makeRequest();
}

main();
