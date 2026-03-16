const express = require('express');
const multer = require('multer');
const path = require('path');
const fs = require('fs').promises;
const crypto = require('crypto');
const { body, validationResult, query } = require('express-validator');
const rateLimit = require('express-rate-limit');
const logger = require('../config/logger');

const router = express.Router();

// Create uploads directory if it doesn't exist
const uploadDir = process.env.UPLOAD_PATH || './storage/uploads/';
async function ensureUploadDir() {
    try {
        await fs.mkdir(uploadDir, { recursive: true });
    } catch (error) {
        logger.error('Failed to create upload directory', error);
    }
}
ensureUploadDir();

// In-memory file storage for testing (replace with database in production)
const fileStorage = new Map();

// Rate limiting for file uploads
const uploadLimiter = rateLimit({
    windowMs: 15 * 60 * 1000, // 15 minutes
    max: 50, // limit each IP to 50 uploads per windowMs
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
        // Allow all file types for backup purposes
        cb(null, true);
    }
});

// Validation middleware
const uploadValidation = [
    body('metadata').optional().isJSON().withMessage('Metadata must be valid JSON'),
    body('fileName').optional().trim().isLength({ min: 1, max: 255 }),
    body('fileSize').optional().isNumeric(),
    body('checksum').optional().isLength({ min: 1, max: 128 })
];

const listValidation = [
    query('limit').optional().isInt({ min: 1, max: 100 }).toInt(),
    query('offset').optional().isInt({ min: 0 }).toInt(),
    query('status').optional().isIn(['pending', 'uploading', 'completed', 'failed']),
    query('search').optional().trim().isLength({ max: 100 })
];

/**
 * Test endpoint
 */
router.get('/test', (req, res) => {
    res.json({
        message: 'File routes are working!',
        timestamp: new Date().toISOString(),
        uploadDir: uploadDir,
        storedFiles: Array.from(fileStorage.keys())
    });
});

/**
 * Upload file endpoint - FIXED to store real files
 * POST /api/files/upload
 */
router.post('/upload', uploadLimiter, upload.single('file'), uploadValidation, async (req, res) => {
    try {
        if (!req.file) {
            return res.status(400).json({
                error: 'No file provided',
                message: 'Please select a file to upload'
            });
        }

        // Parse metadata if provided
        let metadata = {};
        if (req.body.metadata) {
            try {
                metadata = JSON.parse(req.body.metadata);
            } catch (error) {
                logger.warn('Invalid metadata JSON', { metadata: req.body.metadata });
            }
        }

        // Calculate file checksum for verification
        const fileBuffer = await fs.readFile(req.file.path);
        const checksum = crypto.createHash('sha256').update(fileBuffer).digest('hex');

        // Verify file was actually saved
        let fileStats;
        try {
            fileStats = await fs.stat(req.file.path);
            logger.info('File verified on disk', {
                path: req.file.path,
                size: fileStats.size,
                exists: true
            });
        } catch (verifyError) {
            logger.error('File not found on disk after upload', { 
                path: req.file.path, 
                error: verifyError.message 
            });
            return res.status(500).json({
                error: 'Upload failed',
                message: 'File was not saved properly'
            });
        }

        // Generate unique file ID
        const fileId = Date.now() + Math.floor(Math.random() * 1000);

        // Store file metadata in memory (replace with database in production)
        const fileRecord = {
            id: fileId,
            originalName: req.file.originalname,
            storedFileName: req.file.filename,
            filePath: req.file.path,
            fileSize: req.file.size,
            mimeType: req.file.mimetype,
            checksum: checksum,
            uploadStatus: 'completed',
            backupStatus: 'pending',
            metadata: metadata,
            isEncrypted: metadata.encrypted || false,
            uploadedAt: new Date().toISOString()
        };

        fileStorage.set(fileId.toString(), fileRecord);

        logger.info('File uploaded successfully', {
            fileId: fileId,
            fileName: req.file.originalname,
            fileSize: req.file.size,
            filePath: req.file.path,
            checksum: checksum.substring(0, 8) + '...'
        });

        res.status(201).json({
            message: 'File uploaded successfully',
            file: {
                id: fileId,
                originalName: fileRecord.originalName,
                fileName: fileRecord.storedFileName,
                fileSize: fileRecord.fileSize,
                mimeType: fileRecord.mimeType,
                checksum: fileRecord.checksum,
                uploadStatus: fileRecord.uploadStatus,
                backupStatus: fileRecord.backupStatus,
                isEncrypted: fileRecord.isEncrypted,
                uploadedAt: fileRecord.uploadedAt
            }
        });

    } catch (error) {
        // Clean up uploaded file in case of error
        if (req.file?.path) {
            try {
                await fs.unlink(req.file.path);
            } catch (unlinkError) {
                logger.warn('Failed to clean up uploaded file', unlinkError);
            }
        }

        logger.error('File upload error', error);
        res.status(500).json({
            error: 'File upload failed',
            message: 'An internal server error occurred'
        });
    }
});

/**
 * Download file endpoint - FIXED to return actual uploaded files
 * GET /api/files/download/:fileId
 */
router.get('/download/:fileId', async (req, res) => {
    try {
        const { fileId } = req.params;
        
        logger.info('Download request', { fileId });

        // Find file record in storage
        const fileRecord = fileStorage.get(fileId);
        
        if (!fileRecord) {
            logger.warn('File not found in storage', { fileId, availableFiles: Array.from(fileStorage.keys()) });
            
            // Fallback: return enhanced test content with debugging info
            const testContent = `CloudBackup Server - File Download Test

File ID: ${fileId}
Status: File not found in server storage
Available Files: ${Array.from(fileStorage.keys()).join(', ') || 'None'}
Total Stored Files: ${fileStorage.size}
Download Time: ${new Date().toISOString()}

DEBUGGING INFO:
- This indicates either the file was not uploaded properly
- Or the file ID doesn't match any uploaded files
- Check upload process and ensure file IDs are consistent

If you just uploaded a file, use the ID returned in the upload response.

Server Status: Online
Environment: ${process.env.NODE_ENV || 'development'}`;

            res.setHeader('Content-Disposition', `attachment; filename="debug-${fileId}.txt"`);
            res.setHeader('Content-Type', 'text/plain');
            return res.send(testContent);
        }

        // Check if physical file exists on disk
        const filePath = fileRecord.filePath;
        try {
            await fs.access(filePath);
            
            // Get file stats
            const stats = await fs.stat(filePath);
            
            logger.info('Streaming actual uploaded file', { 
                fileId, 
                filePath, 
                originalName: fileRecord.originalName,
                fileSize: stats.size
            });

            // Set proper headers for file download
            res.setHeader('Content-Disposition', `attachment; filename="${fileRecord.originalName}"`);
            res.setHeader('Content-Type', fileRecord.mimeType || 'application/octet-stream');
            res.setHeader('Content-Length', stats.size);

            // Stream the actual uploaded file
            const fileStream = require('fs').createReadStream(filePath);
            
            fileStream.on('error', (error) => {
                logger.error('File stream error', { fileId, error });
                if (!res.headersSent) {
                    res.status(500).json({
                        error: 'Download failed',
                        message: 'Error streaming file'
                    });
                }
            });

            fileStream.on('end', () => {
                logger.info('File download completed successfully', {
                    fileId: fileId,
                    fileName: fileRecord.originalName,
                    fileSize: stats.size
                });
            });

            return fileStream.pipe(res);
            
        } catch (fileError) {
            logger.error('Physical file not found on disk', { 
                fileId, 
                filePath, 
                error: fileError.message 
            });
            
            // File record exists but physical file is missing
            const errorContent = `CloudBackup Server - File Error

File ID: ${fileId}
Original Name: ${fileRecord.originalName}
Status: File record found but physical file missing
Expected Path: ${filePath}
Upload Date: ${fileRecord.uploadedAt}
Error: ${fileError.message}

This suggests the file was uploaded but the physical file was deleted or moved.
Check server file storage and permissions.

Download Time: ${new Date().toISOString()}`;

            res.setHeader('Content-Disposition', `attachment; filename="error-${fileId}.txt"`);
            res.setHeader('Content-Type', 'text/plain');
            return res.send(errorContent);
        }

    } catch (error) {
        logger.error('Download error:', error);
        res.status(500).json({
            error: 'Download failed',
            message: 'An internal server error occurred'
        });
    }
});

/**
 * List files endpoint - FIXED to show actual uploaded files
 * GET /api/files/list
 */
router.get('/list', listValidation, async (req, res) => {
    try {
        // Check validation results
        const errors = validationResult(req);
        if (!errors.isEmpty()) {
            return res.status(400).json({
                error: 'Validation failed',
                details: errors.array()
            });
        }

        const {
            limit = 50,
            offset = 0,
            status,
            search
        } = req.query;

        // Get all stored files
        let files = Array.from(fileStorage.values());

        // Apply filters
        if (status) {
            files = files.filter(file => file.uploadStatus === status);
        }

        if (search) {
            files = files.filter(file => 
                file.originalName.toLowerCase().includes(search.toLowerCase()) ||
                (file.mimeType && file.mimeType.toLowerCase().includes(search.toLowerCase()))
            );
        }

        // Apply pagination
        const total = files.length;
        const paginatedFiles = files
            .sort((a, b) => new Date(b.uploadedAt) - new Date(a.uploadedAt))
            .slice(offset, offset + limit);

        const fileList = paginatedFiles.map(file => ({
            id: file.id,
            originalName: file.originalName,
            fileSize: file.fileSize,
            mimeType: file.mimeType,
            uploadStatus: file.uploadStatus,
            backupStatus: file.backupStatus,
            isEncrypted: file.isEncrypted,
            uploadedAt: file.uploadedAt,
            checksum: file.checksum || null

        }));

        res.json({
            files: fileList,
            pagination: {
                total: total,
                limit: limit,
                offset: offset,
                totalPages: Math.ceil(total / limit),
                currentPage: Math.floor(offset / limit) + 1
            }
        });

    } catch (error) {
        logger.error('File list error', error);
        res.status(500).json({
            error: 'Failed to get file list',
            message: 'An internal server error occurred'
        });
    }
});

/**
 * Delete file endpoint - FIXED to delete actual files
 * DELETE /api/files/:fileId
 */
router.delete('/:fileId', async (req, res) => {
    try {
        const { fileId } = req.params;

        // Find file record
        const fileRecord = fileStorage.get(fileId);

        if (!fileRecord) {
            return res.status(404).json({
                error: 'File not found',
                message: 'The requested file does not exist'
            });
        }

        // Delete physical file from disk
        try {
            await fs.unlink(fileRecord.filePath);
            logger.info('Physical file deleted from disk', { 
                fileId: fileId, 
                filePath: fileRecord.filePath 
            });
        } catch (error) {
            logger.warn('Failed to delete physical file from disk', {
                fileId: fileId,
                filePath: fileRecord.filePath,
                error: error.message
            });
            // Continue with record deletion even if physical file deletion fails
        }

        // Delete file record from memory storage
        fileStorage.delete(fileId);

        logger.info('File deleted successfully', {
            fileId: fileId,
            fileName: fileRecord.originalName
        });

        res.json({
            message: 'File deleted successfully',
            deletedFile: {
                id: fileId,
                originalName: fileRecord.originalName
            }
        });

    } catch (error) {
        logger.error('File deletion error', error);
        res.status(500).json({
            error: 'File deletion failed',
            message: 'An internal server error occurred'
        });
    }
});

/**
 * Get file info endpoint - FIXED to show actual file information
 * GET /api/files/:fileId/info
 */
router.get('/:fileId/info', async (req, res) => {
    try {
        const { fileId } = req.params;

        const fileRecord = fileStorage.get(fileId);

        if (!fileRecord) {
            return res.status(404).json({
                error: 'File not found',
                message: 'The requested file does not exist'
            });
        }

        // Check if physical file still exists
        let physicalFileExists = false;
        let physicalFileSize = 0;
        try {
            const stats = await fs.stat(fileRecord.filePath);
            physicalFileExists = true;
            physicalFileSize = stats.size;
        } catch (error) {
            logger.warn('Physical file check failed', { fileId, error: error.message });
        }

        res.json({
            id: fileRecord.id,
            originalName: fileRecord.originalName,
            storedFileName: fileRecord.storedFileName,
            fileSize: fileRecord.fileSize,
            mimeType: fileRecord.mimeType,
            checksum: fileRecord.checksum,
            uploadStatus: fileRecord.uploadStatus,
            backupStatus: fileRecord.backupStatus,
            isEncrypted: fileRecord.isEncrypted,
            metadata: fileRecord.metadata,
            uploadedAt: fileRecord.uploadedAt,
            filePath: fileRecord.filePath,
            physicalFileExists: physicalFileExists,
            physicalFileSize: physicalFileSize
        });

    } catch (error) {
        logger.error('Get file info error', error);
        res.status(500).json({
            error: 'Failed to get file info',
            message: 'An internal server error occurred'
        });
    }
});

/**
 * Legacy download endpoint for backward compatibility
 * GET /api/files
 */
router.get('/', async (req, res) => {
    try {
        const { user, file } = req.query;

        // For backward compatibility, redirect to new endpoints
        res.json({
            message: 'CloudBackup API - File endpoints',
            availableEndpoints: [
                'GET /api/files/test - Test endpoint',
                'GET /api/files/list - Get file list',
                'GET /api/files/download/:fileId - Download file',
                'GET /api/files/:fileId/info - Get file info',
                'POST /api/files/upload - Upload file',
                'DELETE /api/files/:fileId - Delete file'
            ],
            currentStoredFiles: fileStorage.size,
            uploadDirectory: uploadDir
        });

    } catch (error) {
        logger.error('Legacy endpoint error', error);
        res.status(500).json({
            error: 'Request failed',
            message: 'An internal server error occurred'
        });
    }
});

/**
 * Check if a file with the given SHA-256 checksum already exists.
 * Used by the Python sync client to avoid duplicate uploads.
 * GET /api/files/check-hash/:sha256
 */
router.get('/check-hash/:sha256', async (req, res) => {
    try {
        const { sha256 } = req.params;
 
        // Validate hash format (64 hex chars)
        if (!sha256 || !/^[a-f0-9]{64}$/i.test(sha256)) {
            return res.status(400).json({
                error: 'Invalid hash',
                message: 'SHA-256 hash must be 64 hexadecimal characters'
            });
        }
 
        // Search the in-memory file storage for a matching checksum
        for (const [id, record] of fileStorage.entries()) {
            if (record.checksum && record.checksum.toLowerCase() === sha256.toLowerCase()) {
                logger.info('Hash match found', { sha256: sha256.substring(0, 16), fileId: id });
                return res.json({
                    exists: true,
                    file: {
                        id: record.id,
                        originalName: record.originalName,
                        fileSize: record.fileSize,
                        uploadedAt: record.uploadedAt
                    }
                });
            }
        }
 
        res.json({ exists: false });
 
    } catch (error) {
        logger.error('Check hash error:', error);
        res.status(500).json({
            error: 'Hash check failed',
            message: 'An internal server error occurred'
        });
    }
});

module.exports = router;