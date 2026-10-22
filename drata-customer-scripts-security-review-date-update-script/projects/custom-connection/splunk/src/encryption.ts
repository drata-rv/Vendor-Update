import * as crypto from 'crypto';

export class Encryption {
    private algorithm = 'aes-256-cbc';
    private key = 'DFEHRjdYkgjyGc557ZfUG7gbugzVWZdF'
    private iv = 'Asq9T92CHkVqXYfp';

    constructor() {}

    public encrypt(text: string): string {
        const cipher = crypto.createCipheriv(this.algorithm, this.key, this.iv);
        let encrypted = cipher.update(text, 'utf8', 'hex');
        encrypted += cipher.final('hex');
        return encrypted;
    }
    public decrypt(encrypted: string): string {
        const decipher = crypto.createDecipheriv(this.algorithm, this.key, this.iv);
        let decrypted = decipher.update(encrypted, 'hex', 'utf8');
        decrypted += decipher.final('utf8');
        return decrypted;
    }
}
