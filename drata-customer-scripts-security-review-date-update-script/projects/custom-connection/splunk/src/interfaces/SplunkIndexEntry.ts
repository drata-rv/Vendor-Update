export interface SplunkIndexEntry {
    name: string;
    id: string;
    updated: string;
    links: Record<string, string>;
    author: string;
    acl: {
        app: string;
        can_list: boolean;
        can_write: boolean;
        modifiable: boolean;
        owner: string;
        perms: {
            read: string[];
            write: string[];
        };
        removable: boolean;
        sharing: string;
    };
    fields: {
        required: string[];
        optional: string[];
        wildcard: string[];
    };
    content: Record<string, any>;
}
