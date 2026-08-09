const express = require('express');
const multer = require('multer');
const path = require('path');
const fs = require('fs').promises;
const crypto = require('crypto');
const { body, validationResult, query } = require('express-validator');
const rateLimit = require('express-rate-limit');
const logger = require('../config/logger');

// Sprint 3.6 SECURITY: the file API is now backed by the owner-scoped `files`
// table (models/File.js — user_id UUID NOT NULL, FK to users). The previous
// implementation used a single in-memory Map shared across ALL users, so any
// authenticated caller could list, download, delete, and probe every other
// user's files. That is the cross-user leak: one account's uploads were served
// to and downloaded by another account.
//
// This route is mounted BEHIND verifyToken (server.js), so req.user.id is
// always present. Every handler below scopes strictly to req.user.id, and
// cross-user access returns 404 (never 403 — we do not confirm the file exists).
const { File, Device } = require('../config/database');

const router = express.Router();

const uploadDir = process.env.UPLOAD_PATH || './storage/uploads/';
async function ensureUploadDir() {
    try {
        await fs.mkdir(uploadDir, { recursive: true });
    } catch (error) {
        logger.error('Failed to create upload directory', error);
    }
}
ensureUploadDir();

// Rate limiting for file uploads
const uploadLimiter = rateLimit({
    windowMs: 15 * 60 * 1000, // 15 minutes
    max: 50,
    message: {
        error: 'Too many upload attempts',
        message: 'Please try again later'
    },
    standardHeaders: true,
    legacyHeaders: false,
});

// Configure multer for file uploads
const storage = multer.diskStorage({
    destination: async (req, file, cb) => {
        try {
            await fs.mkdir(uploadDir, { recursive: true });
            cb(null, uploadDir);
        } catch (error) {
            logger.error('Failed to create upload directory', error);
            cb(error);
        }
    },
    filename: (req, file, cb) => {
        const timestamp = Date.now();
        const randomSuffix = crypto.randomBytes(6).toString('hex');
        const sanitizedName = file.originalname.replace(/[^a-zA-Z0-9.-]/g, '_');
        cb(null, `${timestamp}_${randomSuffix}_${sanitizedName}`);
    }
});

const upload = multer({
    storage: storage,
    limits: {
        fileSize: parseInt(process.env.MAX_FILE_SIZE) || (100 * 1024 * 1024), // 100MB
        files: 1
    },
    fileFilter: (req, file, cb) => {
        cb(null, true);
    }
});

// Validation middleware
const uploadValidation = [
    body('metadata').optional().isJSON().withMessage('Metadata must be valid JSON'),
    body('fileName').optional().trim().isLength({ min: 1, max: 255 }),
    body('fileSize').optional().isNumeric(),
    body('checksum').optional().isLength({ min: 1, max: 128 }),
    // Sprint 3.6: device_id is required because the files table has a NOT NULL
    // FK to devices. The client must send the registered device UUID.
    body('deviceId').optional().isUUID().withMessage('deviceId must be a valid UUID')
];

const listValidation = [
    query('limit').optional().isInt({ min: 1, max: 100 }).toInt(),
    query('offset').optional().isInt({ min: 0 }).toInt(),
    query('status').optional().isIn(['PENDING', 'IN_PROGRESS', 'COMPLETED', 'FAILED']),
    query('search').optional().trim().isLength({ max: 100 })
];

// ── Helper: shape a File row for the API response (unchanged field names so
//    the Android client needs no modification) ──────────────────────────────
function toFileResponse(f) {
    return {
        id: f.id,
        originalName: f.filename,
        fileName: path.basename(f.storage_path),
        fileSize: Number(f.file_size),
        mimeType: f.mime_type,
        checksum: f.file_hash,
        uploadStatus: 'completed',
        backupStatus: f.backup_status,
        isEncrypted: f.encryption_key_id != null,
        uploadedAt: f.uploaded_at ? new Date(f.uploaded_at).toISOString() : null
    };
}

// ── Helper: resolve the caller's device. The files table requires a device_id.
//    Prefer an explicit deviceId from the body; otherwise fall back to this
//    user's most-recently-seen active device. Returns null if none exists. ────
async function resolveDeviceId(userId, requestedDeviceId) {
    if (requestedDeviceId) {
        const dev = await Device.findOne({
            where: { id: requestedDeviceId, user_id: userId }
        });
        return dev ? dev.id : null;
    }
    const dev = await Device.findOne({
        where: { user_id: userId, is_active: true },
        order: [['last_seen_at', 'DESC']]
    });
    return dev ? dev.id : null;
}

/**
 * Test endpoint — scoped to the caller.
 */
router.get('/test', async (req, res) => {
    const count = await File.count({ where: { user_id: req.user.id, is_deleted: false } });
    res.json({
        message: 'File routes are working!',
        timestamp: new Date().toISOString(),
        uploadDir: uploadDir,
        yourFileCount: count
    });
});

/**
 * POST /api/files/upload — stores the file owned by req.user.id.
 */
// router.post('/upload', uploadLimiter, upload.single('file'), uploadValidation, async (req, res) => {
//     try {
//         const validationErrors = validationResult(req);
//         if (!validationErrors.isEmpty()) {
//             if (req.file?.path) { try { await fs.unlink(req.file.path); } catch (_) { } }
//             return res.status(400).json({ error: 'Validation failed', details: validationErrors.array() });
//         }

//         if (!req.file) {
//             return res.status(400).json({
//                 error: 'No file provided',
//                 message: 'Please select a file to upload'
//             });
//         }

//         const original = req.file.originalname;
//         if (/^(worker_)?temp_/i.test(original)) {
//             try { await fs.unlink(req.file.path); } catch (_) { }
//             return res.status(422).json({
//                 error: 'Staging artifact rejected',
//                 message: 'Filenames beginning with temp_ or worker_temp_ are internal staging files and are not accepted.'
//             });
//         }

//         const userId = req.user.id;

//         // A device row is mandatory (NOT NULL FK). Resolve before committing.
//         const deviceId = await resolveDeviceId(userId, req.body.deviceId);
//         if (!deviceId) {
//             if (req.file?.path) { try { await fs.unlink(req.file.path); } catch (_) { } }
//             return res.status(400).json({
//                 error: 'No device registered',
//                 message: 'Register a device via POST /api/devices/register before uploading, or pass a valid deviceId.'
//             });
//         }

//         let metadata = {};
//         if (req.body.metadata) {
//             try {
//                 metadata = JSON.parse(req.body.metadata);
//             } catch (error) {
//                 logger.warn('Invalid metadata JSON', { metadata: req.body.metadata });
//             }
//         }

//         // Checksum for dedup / integrity
//         const fileBuffer = await fs.readFile(req.file.path);
//         const checksum = crypto.createHash('sha256').update(fileBuffer).digest('hex');

//         // Verify the file actually landed on disk
//         let fileStats;
//         try {
//             fileStats = await fs.stat(req.file.path);
//         } catch (verifyError) {
//             logger.error('File not found on disk after upload', { path: req.file.path, error: verifyError.message });
//             return res.status(500).json({ error: 'Upload failed', message: 'File was not saved properly' });
//         }

//         // Sprint 3.6 dedup: if THIS user already has this hash, drop the new
//         // physical copy and return the existing record (mirrors the master's
//         // sha256 dedup, scoped per-owner).
//         const existing = await File.findOne({
//             where: { user_id: userId, file_hash: checksum, is_deleted: false }
//         });
//         if (existing) {
//             try { await fs.unlink(req.file.path); } catch (_) { }
//             logger.info('Duplicate upload skipped (hash match)', {
//                 userId, fileId: existing.id, checksum: checksum.substring(0, 8) + '...'
//             });
//             return res.status(200).json({
//                 message: 'File already exists',
//                 file: toFileResponse(existing)
//             });
//         }

//         // The files table has UNIQUE(user_id, file_path). Use the client's
//         // logical path if given, else the original name; disambiguate on clash.
//         const logicalPath = (req.body.fileName || req.file.originalname);

//         const record = await File.create({
//             user_id: userId,
//             device_id: deviceId,
//             file_path: logicalPath,
//             filename: req.file.originalname,
//             file_size: req.file.size,
//             mime_type: req.file.mimetype || 'application/octet-stream',
//             file_hash: checksum,
//             storage_path: req.file.path,
//             metadata: metadata,
//             backup_status: 'COMPLETED',
//             uploaded_at: new Date()
//         });

//         logger.info('File uploaded successfully', {
//             userId, fileId: record.id, fileName: req.file.originalname,
//             fileSize: req.file.size, checksum: checksum.substring(0, 8) + '...'
//         });

//         res.status(201).json({
//             message: 'File uploaded successfully',
//             file: toFileResponse(record)
//         });

//     } catch (error) {
//         if (req.file?.path) {
//             try { await fs.unlink(req.file.path); } catch (unlinkError) {
//                 logger.warn('Failed to clean up uploaded file', unlinkError);
//             }
//         }
//         // UNIQUE(user_id, file_path) collision surfaces here
//         if (error.name === 'SequelizeUniqueConstraintError') {
//             return res.status(409).json({
//                 error: 'Duplicate path',
//                 message: 'A file with this path already exists for your account.'
//             });
//         }
//         logger.error('File upload error', error);
//         res.status(500).json({ error: 'File upload failed', message: 'An internal server error occurred' });
//     }
// });

router.post('/upload', (req, res) => {
    res.status(410).json({
        error: 'Direct upload disabled',
        message: 'RGBC is peer-to-peer. Files upload directly to your master node, not the gateway.'
    });
});

/**
 * GET /api/files/download/:fileId — only if owned by req.user.id.
 */
router.get('/download/:fileId', async (req, res) => {
    try {
        const { fileId } = req.params;
        const userId = req.user.id;

        const record = await File.findOne({
            where: { id: fileId, user_id: userId, is_deleted: false }
        });

        // 404 on both missing AND not-owned — never reveal another user's file exists.
        if (!record) {
            logger.warn('Download denied or not found', { fileId, userId });
            return res.status(404).json({ error: 'File not found', message: 'The requested file does not exist' });
        }

        const filePath = record.storage_path;
        try {
            await fs.access(filePath);
            const stats = await fs.stat(filePath);

            res.setHeader('Content-Disposition', `attachment; filename="${record.filename}"`);
            res.setHeader('Content-Type', record.mime_type || 'application/octet-stream');
            res.setHeader('Content-Length', stats.size);

            const fileStream = require('fs').createReadStream(filePath);
            fileStream.on('error', (error) => {
                logger.error('File stream error', { fileId, error });
                if (!res.headersSent) {
                    res.status(500).json({ error: 'Download failed', message: 'Error streaming file' });
                }
            });
            return fileStream.pipe(res);

        } catch (fileError) {
            logger.error('Physical file missing on disk', { fileId, filePath, error: fileError.message });
            return res.status(410).json({
                error: 'File gone',
                message: 'The file record exists but the stored file is missing.'
            });
        }

    } catch (error) {
        logger.error('Download error:', error);
        res.status(500).json({ error: 'Download failed', message: 'An internal server error occurred' });
    }
});

/**
 * GET /api/files/list — only the caller's files.
 */
router.get('/list', listValidation, async (req, res) => {
    logger.info(`files/list caller: user=${req.user?.id} session=${req.user?.sessionId} ua=${req.get('user-agent')}`);
    try {
        const errors = validationResult(req);
        if (!errors.isEmpty()) {
            return res.status(400).json({ error: 'Validation failed', details: errors.array() });
        }

        const { limit = 50, offset = 0, status, search } = req.query;
        const { Op } = require('sequelize');

        const where = { user_id: req.user.id, is_deleted: false };
        if (status) where.backup_status = status;
        if (search) {
            where[Op.or] = [
                { filename: { [Op.iLike]: `%${search}%` } },
                { mime_type: { [Op.iLike]: `%${search}%` } }
            ];
        }

        const { count, rows } = await File.findAndCountAll({
            where,
            order: [['uploaded_at', 'DESC']],
            limit,
            offset
        });

        res.json({
            files: rows.map(toFileResponse),
            pagination: {
                total: count,
                limit,
                offset,
                totalPages: Math.ceil(count / limit),
                currentPage: Math.floor(offset / limit) + 1
            }
        });

    } catch (error) {
        logger.error('File list error', error);
        res.status(500).json({ error: 'Failed to get file list', message: 'An internal server error occurred' });
    }
});

/**
 * DELETE /api/files/:fileId — only if owned by req.user.id. Soft-delete.
 */
router.delete('/:fileId', async (req, res) => {
    try {
        const { fileId } = req.params;
        const userId = req.user.id;

        const record = await File.findOne({
            where: { id: fileId, user_id: userId, is_deleted: false }
        });

        if (!record) {
            return res.status(404).json({ error: 'File not found', message: 'The requested file does not exist' });
        }

        // Remove the physical file, then soft-delete the record.
        try {
            await fs.unlink(record.storage_path);
        } catch (error) {
            logger.warn('Failed to delete physical file', { fileId, error: error.message });
        }

        await record.update({ is_deleted: true });

        logger.info('File deleted', { userId, fileId, fileName: record.filename });
        res.json({
            message: 'File deleted successfully',
            deletedFile: { id: record.id, originalName: record.filename }
        });

    } catch (error) {
        logger.error('File deletion error', error);
        res.status(500).json({ error: 'File deletion failed', message: 'An internal server error occurred' });
    }
});

/**
 * GET /api/files/:fileId/info — only if owned by req.user.id.
 */
router.get('/:fileId/info', async (req, res) => {
    try {
        const { fileId } = req.params;
        const userId = req.user.id;

        const record = await File.findOne({
            where: { id: fileId, user_id: userId, is_deleted: false }
        });

        if (!record) {
            return res.status(404).json({ error: 'File not found', message: 'The requested file does not exist' });
        }

        let physicalFileExists = false;
        let physicalFileSize = 0;
        try {
            const stats = await fs.stat(record.storage_path);
            physicalFileExists = true;
            physicalFileSize = stats.size;
        } catch (error) {
            logger.warn('Physical file check failed', { fileId, error: error.message });
        }

        res.json({
            ...toFileResponse(record),
            metadata: record.metadata,
            // storage_path deliberately NOT exposed — it's a server-internal path.
            physicalFileExists,
            physicalFileSize
        });

    } catch (error) {
        logger.error('Get file info error', error);
        res.status(500).json({ error: 'Failed to get file info', message: 'An internal server error occurred' });
    }
});

/**
 * GET /api/files — endpoint index (no data).
 */
router.get('/', async (req, res) => {
    res.json({
        message: 'CloudBackup API - File endpoints',
        availableEndpoints: [
            'GET /api/files/test',
            'GET /api/files/list',
            'GET /api/files/download/:fileId',
            'GET /api/files/:fileId/info',
            'POST /api/files/upload',
            'DELETE /api/files/:fileId',
            'GET /api/files/check-hash/:sha256'
        ]
    });
});

/**
 * GET /api/files/check-hash/:sha256 — dedup probe, scoped to the caller.
 * SECURITY: only ever reports on the CALLER's own files. A global probe would
 * let one user test whether another user has a given file (existence oracle).
 */
router.get('/check-hash/:sha256', async (req, res) => {
    try {
        const { sha256 } = req.params;
        if (!sha256 || !/^[a-f0-9]{64}$/i.test(sha256)) {
            return res.status(400).json({
                error: 'Invalid hash',
                message: 'SHA-256 hash must be 64 hexadecimal characters'
            });
        }

        const record = await File.findOne({
            where: { user_id: req.user.id, file_hash: sha256.toLowerCase(), is_deleted: false }
        });

        if (record) {
            return res.json({ exists: true, file: toFileResponse(record) });
        }
        res.json({ exists: false });

    } catch (error) {
        logger.error('Check hash error:', error);
        res.status(500).json({ error: 'Hash check failed', message: 'An internal server error occurred' });
    }
});

module.exports = router;