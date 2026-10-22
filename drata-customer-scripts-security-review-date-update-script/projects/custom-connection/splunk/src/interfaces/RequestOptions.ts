export interface RequestOptions {
    hostname: string;
    port: number;
    path: string;
    method: string;
    headers: Record<string, string>;
    rejectUnauthorized: boolean;
}
