import { SplunkIndexEntry } from './SplunkIndexEntry';

export interface SplunkResponse {
    links: Record<string, string>;
    origin: string;
    updated: string;
    generator: {
        build: string;
        version: string;
    };
    entry: SplunkIndexEntry[];
    paging: {
        total: number;
        perPage: number;
        offset: number;
    };
    messages: any[];
}
