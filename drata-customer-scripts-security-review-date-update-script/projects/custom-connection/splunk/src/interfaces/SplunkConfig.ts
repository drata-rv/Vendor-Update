export interface SplunkConfig {
    hostname: string;
    port: number;
    path: string;
    username: string;
    password?: string;
    passwordEncrypted?: string;
}
